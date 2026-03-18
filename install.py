#!/usr/bin/env python3
"""
Darkflame Universe - Pterodactyl Python Container

Architecture des ports :
  Auth   : binaire écoute directement sur 25749 (public)
  Master : binaire écoute directement sur 25651 (public)
  Chat   : binaire écoute directement sur 25690 (public)
  World  : binaires écoutent sur 127.0.0.1:3000, 3001, 3002... (internes)
           proxy UDP Python écoute sur 0.0.0.0:25631 (public)
           le proxy patche les paquets de transfert de zone pour que le
           client reçoive toujours 38.190.133.136:25631
"""

import os
import subprocess
import sys
import shutil
import zipfile
import configparser
import urllib.request
import tarfile
import io
import uuid
import socket
import threading
import time
import struct

def pip_install(pkg):
    subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=True)

try:
    import pymysql
except ImportError:
    print("[=] Installation de pymysql...")
    pip_install("pymysql")
    os.execv(sys.executable, [sys.executable] + sys.argv)

try:
    import bcrypt
except ImportError:
    print("[=] Installation de bcrypt...")
    pip_install("bcrypt")
    os.execv(sys.executable, [sys.executable] + sys.argv)

try:
    import zstandard as zstd
except ImportError:
    print("[=] Installation de zstandard...")
    pip_install("zstandard")
    os.execv(sys.executable, [sys.executable] + sys.argv)

HOME_DIR     = "/home/container"
BINS_ZIP     = os.path.join(HOME_DIR, "darkflame-bins.zip")
BUILD_DIR    = os.path.join(HOME_DIR, "darkflame-build")
SERVER_DIR   = os.path.join(HOME_DIR, "DarkflameServer")
CONFIG_FILE  = os.path.join(HOME_DIR, "config_template.ini")
REPO_DIR     = os.path.join(HOME_DIR, "darkflame-python")
GLIBC_DIR    = os.path.join(HOME_DIR, "glibc-compat")
PATCHELF     = os.path.join(HOME_DIR, "patchelf")
PATCHED_FLAG = os.path.join(HOME_DIR, ".glibc_patched")
EXTRACT_FLAG = os.path.join(HOME_DIR, ".bins_extracted")
PENDING_ACCOUNT_FLAG = os.path.join(HOME_DIR, ".pending_first_account")

DFS_TARBALL_URL = "https://github.com/DarkflameUniverse/DarkflameServer/archive/refs/heads/main.tar.gz"

DFS_PLAIN_DIRS = [
    ("migrations", "migrations"),
    ("vanity",     "vanity"),
]

BINARY_NAMES = {
    "master": ["MasterServer", "masterserver"],
    "auth":   ["AuthServer",   "authserver"],
    "chat":   ["ChatServer",   "chatserver"],
    "world":  ["WorldServer",  "worldserver"],
}

SEC = "http://security.ubuntu.com/ubuntu/pool/main"
GLIBC_DEBS = [
    f"{SEC}/g/glibc/libc6_2.39-0ubuntu8.7_amd64.deb",
    f"{SEC}/g/gcc-14/libstdc++6_14.2.0-4ubuntu2~24.04.1_amd64.deb",
    f"{SEC}/g/gcc-14/libgcc-s1_14.2.0-4ubuntu2~24.04.1_amd64.deb",
]
PATCHELF_URL = "https://github.com/NixOS/patchelf/releases/download/0.18.0/patchelf-0.18.0-x86_64.tar.gz"

DEFAULT_AUTH_PORT   = "25749"
DEFAULT_MASTER_PORT = "25651"
DEFAULT_CHAT_PORT   = "25690"
DEFAULT_WORLD_PORT  = "25631"   # port public unique pour tous les worlds
WORLD_PORT_INTERNAL_START = 3000  # les binaires world écoutent ici
WORLD_PORT_RANGE          = 50    # 3000..3049


# ---------------------------------------------------------------------------
# Proxy UDP World
# ---------------------------------------------------------------------------
# DFU alloue world_port_start, world_port_start+1, world_port_start+2...
# pour chaque WorldServer. Ces ports écoutent sur localhost (inaccessibles).
# Le proxy :
#   - écoute sur 0.0.0.0:25631
#   - maintient une table session client -> backend interne
#   - quand le MasterServer envoie un paquet "redirect vers port interne X",
#     le proxy patche ce paquet pour mettre 38.190.133.136:25631 à la place
# ---------------------------------------------------------------------------

def patch_world_redirect(data: bytes, internal_ports: set,
                         public_ip: str, public_port: int) -> bytes:
    """
    Cherche le motif [u8 ip_len][IPv4 ascii][u16_LE port] dans data.
    Si port est un port interne world, remplace par public_ip:public_port.
    """
    result = bytearray(data)
    i = 0
    patched = False
    while i < len(result) - 3:
        ip_len = result[i]
        if 7 <= ip_len <= 15:
            end_ip = i + 1 + ip_len
            if end_ip + 2 <= len(result):
                try:
                    candidate_ip = result[i+1:end_ip].decode('ascii')
                except Exception:
                    i += 1
                    continue
                parts = candidate_ip.split('.')
                if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    port_val = struct.unpack_from('<H', bytes(result[end_ip:end_ip+2]))[0]
                    if port_val in internal_ports:
                        new_ip_b = public_ip.encode('ascii')
                        replacement = bytes([len(new_ip_b)]) + new_ip_b + struct.pack('<H', public_port)
                        result = result[:i] + bytearray(replacement) + result[end_ip+2:]
                        i += len(replacement)
                        patched = True
                        continue
        i += 1
    if patched:
        print(f"[proxy-world] Patch redirect → {public_ip}:{public_port}")
    return bytes(result)


class WorldProxy:
    """Proxy UDP : public 25631 <-> internes 3000..3049.
    
    Table de sessions : addr_client -> port_interne_world
    Quand un nouveau client arrive, on cherche le WorldServer actif
    (port ouvert sur localhost) avec le moins de sessions.
    """
    SESSION_TIMEOUT = 120

    def __init__(self, public_port: int, internal_start: int,
                 internal_range: int, public_ip: str):
        self.public_port    = public_port
        self.int_start      = internal_start
        self.int_range      = internal_range
        self.public_ip      = public_ip
        self.internal_ports = set(range(internal_start, internal_start + internal_range))

        self.sock      = None
        self.sessions  = {}   # client_addr -> internal_port
        self.last_seen = {}   # client_addr -> timestamp
        self.bsocks    = {}   # internal_port -> UDP socket
        self.lock      = threading.Lock()

    def _get_active_ports(self):
        """Retourne les ports internes avec un WorldServer actif (via /proc/net/udp)."""
        active = set()
        try:
            with open('/proc/net/udp') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    try:
                        p = int(parts[1].split(':')[1], 16)
                        if p in self.internal_ports:
                            active.add(p)
                    except Exception:
                        pass
        except Exception:
            pass
        return active

    def _pick_backend(self):
        """Choisit le port interne actif avec le moins de sessions."""
        active = self._get_active_ports()
        if not active:
            return self.int_start  # fallback
        with self.lock:
            load = {p: sum(1 for v in self.sessions.values() if v == p) for p in active}
        return min(load, key=load.get)

    def _backend_sock(self, port):
        if port not in self.bsocks:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            self.bsocks[port] = s
        return self.bsocks[port]

    def _backend_reader(self, port):
        """Thread : reçoit les paquets d'un WorldServer interne et les renvoie au client."""
        bsock = self._backend_sock(port)
        while True:
            try:
                data, _ = bsock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception:
                break

            # Patche les paquets de redirection de zone
            data = patch_world_redirect(
                data, self.internal_ports, self.public_ip, self.public_port
            )

            with self.lock:
                clients = [c for c, p in self.sessions.items() if p == port]
            for addr in clients:
                try:
                    self.sock.sendto(data, addr)
                except Exception:
                    pass

    def _cleanup_loop(self):
        while True:
            time.sleep(30)
            now = time.time()
            with self.lock:
                dead = [c for c, t in self.last_seen.items()
                        if now - t > self.SESSION_TIMEOUT]
                for c in dead:
                    self.sessions.pop(c, None)
                    self.last_seen.pop(c, None)

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.public_port))
        print(f"[proxy-world] Écoute 0.0.0.0:{self.public_port} → internes {self.int_start}..{self.int_start+self.int_range-1}")

        for port in range(self.int_start, self.int_start + self.int_range):
            threading.Thread(target=self._backend_reader, args=(port,), daemon=True).start()

        threading.Thread(target=self._cleanup_loop, daemon=True).start()

        while True:
            try:
                data, client_addr = self.sock.recvfrom(65535)
            except Exception:
                continue

            with self.lock:
                self.last_seen[client_addr] = time.time()
                if client_addr not in self.sessions:
                    backend = self._pick_backend()
                    self.sessions[client_addr] = backend
                    print(f"[proxy-world] Nouvelle session {client_addr} → :{backend}")
                backend = self.sessions[client_addr]

            try:
                self._backend_sock(backend).sendto(data, ("127.0.0.1", backend))
            except Exception as e:
                print(f"[proxy-world] Erreur envoi :{backend}: {e}")


def start_world_proxy(public_port: int, public_ip: str):
    proxy = WorldProxy(public_port, WORLD_PORT_INTERNAL_START,
                       WORLD_PORT_RANGE, public_ip)
    t = threading.Thread(target=proxy.start, daemon=True)
    t.start()
    print(f"[proxy-world] Démarré : public {public_port} → internes {WORLD_PORT_INTERNAL_START}..{WORLD_PORT_INTERNAL_START+WORLD_PORT_RANGE-1}")
    return proxy


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db_conn(cfg):
    return pymysql.connect(
        host=cfg.get("Database", "mysql_host"),
        port=int(cfg.get("Database", "mysql_port", fallback="3306")),
        user=cfg.get("Database", "mysql_username"),
        password=cfg.get("Database", "mysql_password"),
        database=cfg.get("Database", "mysql_database"),
        connect_timeout=10,
        autocommit=True,
    )


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def db_tables_ready(cfg) -> bool:
    conn = get_db_conn(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name IN ('accounts', 'play_keys');",
                (cfg.get("Database", "mysql_database"),)
            )
            return cur.fetchone()[0] == 2
    finally:
        conn.close()


def create_play_key(cursor) -> int:
    key_string = str(uuid.uuid4()).upper()[:20]
    cursor.execute(
        "INSERT INTO play_keys (key_string, created_at, key_uses, active) VALUES (%s, NOW(), 99, 1);",
        (key_string,)
    )
    return cursor.lastrowid


def create_account(cfg, username: str, password: str, gm_level: int = 9, with_play_key: bool = True):
    conn = get_db_conn(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM accounts WHERE name = %s LIMIT 1;", (username,))
            if cur.fetchone():
                print(f"[!] Le compte '{username}' existe déjà, skip.")
                return False
            hashed = hash_password(password)
            if with_play_key:
                play_key_id = create_play_key(cur)
                cur.execute(
                    "INSERT INTO accounts (name, password, gm_level, play_key_id) VALUES (%s, %s, %s, %s);",
                    (username, hashed, gm_level, play_key_id)
                )
                print(f"[✓] Compte '{username}' créé (gm_level={gm_level}, play_key_id={play_key_id}).")
            else:
                cur.execute(
                    "INSERT INTO accounts (name, password, gm_level) VALUES (%s, %s, %s);",
                    (username, hashed, gm_level)
                )
                print(f"[✓] Compte '{username}' créé (gm_level={gm_level}, sans play key).")
        return True
    finally:
        conn.close()


def setup_first_account(cfg):
    admin_user = os.environ.get("ADMIN_USERNAME", "").strip()
    admin_pass = os.environ.get("ADMIN_PASSWORD", "").strip()

    if not admin_user or not admin_pass:
        if os.path.isfile(PENDING_ACCOUNT_FLAG):
            os.remove(PENDING_ACCOUNT_FLAG)
        return

    tables_ok = db_tables_ready(cfg)

    if not tables_ok:
        print("[=] Tables DB absentes → MasterServer va les créer.")
        with open(PENDING_ACCOUNT_FLAG, 'w') as f:
            f.write(f"{admin_user}\n{admin_pass}\n9\n")
        return

    conn = get_db_conn(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM accounts;")
            count = cur.fetchone()[0]
    finally:
        conn.close()

    if count > 0:
        print(f"[✓] {count} compte(s) existant(s) en DB, skip création admin.")
        if os.path.isfile(PENDING_ACCOUNT_FLAG):
            os.remove(PENDING_ACCOUNT_FLAG)
        return

    print("\n" + "=" * 50)
    print(" CRÉATION DU COMPTE ADMINISTRATEUR")
    print("=" * 50)

    username, password, gm_level = admin_user, admin_pass, 9
    if os.path.isfile(PENDING_ACCOUNT_FLAG):
        try:
            lines = open(PENDING_ACCOUNT_FLAG).read().splitlines()
            if len(lines) >= 3:
                username = lines[0].strip() or admin_user
                password = lines[1].strip() or admin_pass
                gm_level = int(lines[2].strip())
        except Exception:
            pass

    create_account(cfg, username, password, gm_level=gm_level, with_play_key=True)
    print("[✓] Compte admin prêt.")

    if os.path.isfile(PENDING_ACCOUNT_FLAG):
        os.remove(PENDING_ACCOUNT_FLAG)


def cmd_add_account(cfg, args):
    if len(args) < 2:
        print("Usage : python install.py --add-account <nom> <mdp> [--gm <niveau>]")
        sys.exit(1)

    username = args[0]
    password = args[1]
    gm_level = 0

    if "--gm" in args:
        idx = args.index("--gm")
        if idx + 1 < len(args):
            try:
                gm_level = int(args[idx + 1])
            except ValueError:
                pass

    if not db_tables_ready(cfg):
        print("[!] ERREUR : les tables DB n'existent pas encore.")
        sys.exit(1)

    create_account(cfg, username, password, gm_level=gm_level, with_play_key=True)
    sys.exit(0)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def refresh_config_template():
    repo_cfg = os.path.join(REPO_DIR, "config_template.ini")
    if os.path.isfile(repo_cfg):
        shutil.copy2(repo_cfg, CONFIG_FILE)
        print(f"[✓] config_template.ini mis à jour depuis le repo.")
    elif not os.path.isfile(CONFIG_FILE):
        print(f"[!] ERREUR : {CONFIG_FILE} introuvable !")
        sys.exit(1)


def load_config():
    refresh_config_template()

    env_map = {
        "MYSQL_HOST":             ("Database",   "mysql_host"),
        "MYSQL_PORT":             ("Database",   "mysql_port"),
        "MYSQL_DATABASE":         ("Database",   "mysql_database"),
        "MYSQL_USER":             ("Database",   "mysql_username"),
        "MYSQL_PASSWORD":         ("Database",   "mysql_password"),
        "CLIENT_PATH":            ("General",    "client_location"),
        "USE_CUSTOM_FDB":         ("General",    "use_custom_fdb"),
        "FDB_PATH":               ("General",    "fdb_path"),
        "EXTERNAL_IP":            ("Networking", "external_ip"),
        "AUTH_SERVER_PORT":       ("Networking", "auth_server_port"),
        "WORLD_SERVER_PORT":      ("Networking", "world_server_port"),
        "CHAT_SERVER_PORT":       ("Networking", "chat_server_port"),
        "MASTER_SERVER_PORT":     ("Networking", "master_server_port"),
        "MAX_OFFLINE_TIME":       ("Gameplay",   "max_offline_time"),
        "KICK_AFTER_FAILED_AUTH": ("Gameplay",   "kick_after_failed_auth"),
        "ALLOW_MYTHRAN_COMMANDS": ("Gameplay",   "allow_mythran_commands"),
        "DISABLE_ANTI_CHEAT":     ("Gameplay",   "disable_anti_cheat"),
        "CHATBOT_ENABLED":        ("Gameplay",   "chatbot_enabled"),
        "LOG_ACTIVITY":           ("Gameplay",   "log_activity"),
        "LOG_LEVEL":              ("Logging",    "log_level"),
        "LOG_TO_CONSOLE":         ("Logging",    "log_to_console"),
        "LOG_TO_FILE":            ("Logging",    "log_to_file"),
    }

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)

    for env_key, (section, option) in env_map.items():
        val = os.environ.get(env_key)
        if val:
            if not cfg.has_section(section):
                cfg.add_section(section)
            cfg.set(section, option, val)

    return cfg


def find_binary(name_key):
    search_dirs = [BUILD_DIR]
    if os.path.isdir(BUILD_DIR):
        for entry in os.listdir(BUILD_DIR):
            sub = os.path.join(BUILD_DIR, entry)
            if os.path.isdir(sub):
                search_dirs.append(sub)
    for name in BINARY_NAMES[name_key]:
        for d in search_dirs:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    return None


def run(cmd, cwd=None, check=True):
    print(f"[+] {cmd}")
    return subprocess.run(cmd, shell=True, cwd=cwd, check=check)


def needs_glibc_compat():
    master = find_binary("master")
    if master is None:
        return False
    r = subprocess.run(["ldd", master], capture_output=True, text=True)
    if r.returncode != 0:
        return True
    output = r.stdout + r.stderr
    return "GLIBC_2.38" in output or "GLIBC_2.39" in output or "not found" in output


def download_file(url, dest):
    print(f"[=] Téléchargement {url.split('/')[-1]}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Wget/1.21"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)


def extract_so_from_tar(t, out_dir):
    members = t.getmembers()
    for m in members:
        base = os.path.basename(m.name)
        if not base:
            continue
        if ".so" not in base and not (base.startswith("ld-") and base.endswith(".so")):
            continue
        if not m.isfile():
            continue
        dest_path = os.path.join(out_dir, base)
        try:
            src = t.extractfile(m)
            if src:
                with open(dest_path, 'wb') as f:
                    shutil.copyfileobj(src, f)
                os.chmod(dest_path, 0o755)
        except Exception as e:
            print(f"  [!] Erreur extraction {base}: {e}")
    for m in members:
        base = os.path.basename(m.name)
        if not base or not m.issym():
            continue
        if ".so" not in base and not base.startswith("ld-"):
            continue
        dest_path   = os.path.join(out_dir, base)
        link_target = os.path.basename(m.linkname)
        target_path = os.path.join(out_dir, link_target)
        if link_target == base or not os.path.isfile(target_path):
            continue
        try:
            if os.path.lexists(dest_path):
                os.remove(dest_path)
            os.symlink(link_target, dest_path)
        except Exception as e:
            print(f"  [!] Erreur symlink {base}: {e}")


def extract_tar_zst(zst_path, out_dir):
    dctx = zstd.ZstdDecompressor()
    with open(zst_path, 'rb') as f:
        tar_bytes = dctx.stream_reader(f).read()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as t:
        extract_so_from_tar(t, out_dir)


def _extract_deb_ar(deb_path, work_dir):
    with open(deb_path, 'rb') as f:
        if f.read(8) != b'!<arch>\n':
            return None
        while True:
            header = f.read(60)
            if len(header) < 60:
                break
            name = header[0:16].strip().decode('ascii', errors='ignore').rstrip('/')
            size = int(header[48:58].strip())
            data = f.read(size)
            if size % 2 == 1:
                f.read(1)
            if name.startswith('data.tar'):
                out = os.path.join(work_dir, name)
                with open(out, 'wb') as g:
                    g.write(data)
                return out
    return None


def extract_deb_libs(deb_path, out_dir):
    work = deb_path + ".work"
    os.makedirs(work, exist_ok=True)
    r = subprocess.run(["ar", "x", deb_path], cwd=work, capture_output=True)
    if r.returncode != 0:
        data_tar = _extract_deb_ar(deb_path, work)
    else:
        data_tar = next((os.path.join(work, f) for f in os.listdir(work)
                         if f.startswith("data.tar")), None)
    if data_tar is None:
        print(f"[!] data.tar introuvable dans {os.path.basename(deb_path)}")
        shutil.rmtree(work, ignore_errors=True)
        return
    print(f"[=] Extraction {os.path.basename(data_tar)}...")
    if data_tar.endswith(".zst"):
        extract_tar_zst(data_tar, out_dir)
    else:
        with tarfile.open(data_tar) as t:
            extract_so_from_tar(t, out_dir)
    shutil.rmtree(work, ignore_errors=True)


def find_ld(glibc_dir):
    for f in os.listdir(glibc_dir):
        if f.startswith("ld-2.") and f.endswith(".so"):
            p = os.path.join(glibc_dir, f)
            if os.path.isfile(p) and not os.path.islink(p):
                os.chmod(p, 0o755)
                return p
    for f in os.listdir(glibc_dir):
        if "ld-linux" in f:
            p = os.path.join(glibc_dir, f)
            if os.path.islink(p):
                target_name = os.path.basename(os.readlink(p))
                real = os.path.join(glibc_dir, target_name)
                if target_name != f and os.path.isfile(real):
                    os.chmod(real, 0o755)
                    return p
            elif os.path.isfile(p):
                os.chmod(p, 0o755)
                return p
    for f in os.listdir(glibc_dir):
        if f.startswith("ld-"):
            p = os.path.realpath(os.path.join(glibc_dir, f))
            if os.path.isfile(p):
                os.chmod(p, 0o755)
                return p
    return None


def setup_glibc_compat():
    if os.path.isfile(PATCHED_FLAG):
        print("[✓] GLIBC compat déjà appliqué, skip.")
        return

    if not os.path.isdir(GLIBC_DIR) or not any('libc' in f for f in os.listdir(GLIBC_DIR)):
        print("[=] GLIBC_2.38 requis → téléchargement libs Ubuntu 24.04...")
        shutil.rmtree(GLIBC_DIR, ignore_errors=True)
        os.makedirs(GLIBC_DIR, exist_ok=True)
        tmp = os.path.join(HOME_DIR, "glibc-tmp")
        os.makedirs(tmp, exist_ok=True)
        for url in GLIBC_DEBS:
            deb = os.path.join(tmp, url.split('/')[-1])
            try:
                download_file(url, deb)
                extract_deb_libs(deb, GLIBC_DIR)
            except Exception as e:
                print(f"[!] Erreur sur {url.split('/')[-1]}: {e}")
        shutil.rmtree(tmp, ignore_errors=True)

    if not any('libc' in f for f in os.listdir(GLIBC_DIR)):
        print("[!] ERREUR : libc.so.6 non trouvé")
        sys.exit(1)

    if not os.path.isfile(PATCHELF):
        print("[=] Téléchargement patchelf...")
        tar = os.path.join(HOME_DIR, "patchelf.tar.gz")
        download_file(PATCHELF_URL, tar)
        with tarfile.open(tar) as t:
            for m in t.getmembers():
                if m.name.endswith("patchelf") and m.isfile():
                    with open(PATCHELF, 'wb') as out_f:
                        shutil.copyfileobj(t.extractfile(m), out_f)
                    break
        os.chmod(PATCHELF, 0o755)
        os.remove(tar)

    ld = find_ld(GLIBC_DIR)
    if ld is None:
        print(f"[!] ld-linux introuvable.")
        sys.exit(1)

    mariadb_lib = os.path.join(BUILD_DIR, "thirdparty", "mariadb-connector-cpp",
                               "src", "mariadb_connector_cpp-build")
    rpath = f"{GLIBC_DIR}:{BUILD_DIR}:{mariadb_lib}"

    patched_count = 0
    for key in BINARY_NAMES:
        b = find_binary(key)
        if b:
            print(f"[=] Patch {os.path.basename(b)} → GLIBC compat...")
            if run(f"{PATCHELF} --set-interpreter {ld} --set-rpath {rpath} {b}", check=False).returncode == 0:
                patched_count += 1

    if patched_count > 0:
        open(PATCHED_FLAG, 'w').close()
        print(f"[✓] {patched_count} binaire(s) patché(s) avec GLIBC 2.39.")


def setup_ld_library_path():
    paths = [BUILD_DIR, GLIBC_DIR]
    for root, dirs, files in os.walk(BUILD_DIR):
        for f in files:
            if f.endswith(".so") or ".so." in f:
                paths.append(root)
                break
    current = os.environ.get("LD_LIBRARY_PATH", "")
    new_path = ":".join(dict.fromkeys(paths)) + (":" + current if current else "")
    os.environ["LD_LIBRARY_PATH"] = new_path
    print(f"[=] LD_LIBRARY_PATH={new_path}")


def extract_prebuilt():
    print("\n[=] darkflame-bins.zip détecté → mode binaires pré-compilés")
    if os.path.isfile(EXTRACT_FLAG) and find_binary("master") is not None:
        print("[✓] Binaires déjà extraits, skip.")
        return
    if os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    if os.path.isfile(PATCHED_FLAG):
        os.remove(PATCHED_FLAG)
    print("[=] Extraction des binaires...")
    os.makedirs(BUILD_DIR, exist_ok=True)
    with zipfile.ZipFile(BINS_ZIP, 'r') as z:
        z.extractall(BUILD_DIR)
    for key in BINARY_NAMES:
        for name in BINARY_NAMES[key]:
            p = os.path.join(BUILD_DIR, name)
            if os.path.isfile(p):
                os.chmod(p, 0o755)
    open(EXTRACT_FLAG, 'w').close()
    print("[✓] Binaires extraits dans", BUILD_DIR)


def _extract_dir_from_tar(t, members, src_prefix, dest_dir):
    os.makedirs(dest_dir, exist_ok=True)
    count = 0
    for m in members:
        if not m.name.startswith(src_prefix):
            continue
        rel = m.name[len(src_prefix):]
        if not rel:
            continue
        dest_path = os.path.join(dest_dir, rel)
        if m.isdir():
            os.makedirs(dest_path, exist_ok=True)
        elif m.isfile():
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            src = t.extractfile(m)
            if src:
                with open(dest_path, 'wb') as f:
                    shutil.copyfileobj(src, f)
                count += 1
    return count


def fetch_repo_resources():
    missing_dirs = [(src, dst) for src, dst in DFS_PLAIN_DIRS
                    if not os.path.isdir(os.path.join(BUILD_DIR, dst))]
    nav_dir = os.path.join(BUILD_DIR, "navmeshes")
    need_navmeshes = not (os.path.isdir(nav_dir)
                          and any(f.endswith(".bin") for f in os.listdir(nav_dir)))

    if not missing_dirs and not need_navmeshes:
        print("[✓] Tous les dossiers repo déjà présents, skip.")
        return

    tar_path = os.path.join(HOME_DIR, "dfs-main.tar.gz")
    print("[=] Téléchargement DarkflameServer (tarball)...")
    try:
        download_file(DFS_TARBALL_URL, tar_path)
    except Exception as e:
        print(f"[!] Échec téléchargement : {e}")
        sys.exit(1)

    with tarfile.open(tar_path, 'r:gz') as t:
        members = t.getmembers()
        root_prefix = ""
        for m in members:
            parts = m.name.split('/')
            if len(parts) >= 2:
                root_prefix = parts[0] + "/"
                break

        for src_name, dst_name in missing_dirs:
            count = _extract_dir_from_tar(t, members, root_prefix + src_name + "/",
                                           os.path.join(BUILD_DIR, dst_name))
            print(f"[✓] {dst_name}/ : {count} fichier(s)")

        if need_navmeshes:
            nav_zip_path = root_prefix + "resources/navmeshes.zip"
            nav_member = next((m for m in members if m.name == nav_zip_path), None)
            if nav_member:
                nav_zip_data = t.extractfile(nav_member).read()
                os.makedirs(nav_dir, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(nav_zip_data)) as z:
                    for name in z.namelist():
                        fname = os.path.basename(name)
                        if fname.endswith(".bin"):
                            with z.open(name) as src, open(os.path.join(nav_dir, fname), 'wb') as out:
                                shutil.copyfileobj(src, out)
                print(f"[✓] navmeshes/ : {len([f for f in os.listdir(nav_dir) if f.endswith('.bin')])} .bin")

    os.remove(tar_path)
    print("[✓] Ressources OK.")


def setup_server_data(cfg):
    print("\n[=] Vérification des données serveur...")
    client_path = cfg.get("General", "client_location", fallback="/home/container/client")

    for sub in ["res", "locale"]:
        src = os.path.join(client_path, sub)
        dst = os.path.join(BUILD_DIR, sub)
        if os.path.isdir(src):
            if os.path.exists(dst):
                print(f"[✓] {sub}/ déjà présent, skip.")
            else:
                print(f"[=] Copie {sub}/ ...")
                shutil.copytree(src, dst)
        else:
            print(f"[!] ERREUR : {src} introuvable !")
            sys.exit(1)

    fetch_repo_resources()
    os.makedirs(os.path.join(BUILD_DIR, "logs"), exist_ok=True)
    print("[✓] Données serveur OK.")


def install_runtime_deps():
    print("\n[=] Vérification des dépendances runtime...")
    r = subprocess.run("apt-get update -qq", shell=True, capture_output=True)
    if r.returncode != 0:
        print("[!] apt-get indisponible, on continue.")
        return
    subprocess.run("apt-get install -y libmariadb3 libssl3 unzip binutils", shell=True)


def install_build_deps():
    r = subprocess.run("apt-get update -qq", shell=True, capture_output=True)
    if r.returncode != 0:
        print("[!] ERREUR : apt-get non disponible. Uploadez darkflame-bins.zip.")
        sys.exit(1)
    run("apt-get install -y git cmake g++ zlib1g-dev libssl-dev libmariadb-dev-compat libmariadb-dev unzip")


def clone_server():
    if os.path.exists(SERVER_DIR):
        run("git pull", cwd=SERVER_DIR)
    else:
        run(f"git clone --recursive https://github.com/DarkflameUniverse/DarkflameServer.git {SERVER_DIR}")


def build_server():
    if find_binary("master") is not None:
        print("[✓] Déjà compilé, skip.")
        return
    os.makedirs(BUILD_DIR, exist_ok=True)
    run("cmake .. -DCMAKE_BUILD_TYPE=Release", cwd=BUILD_DIR)
    run("make -j$(nproc)", cwd=BUILD_DIR)
    keep = set(sum(BINARY_NAMES.values(), []))
    for item in os.listdir(BUILD_DIR):
        p = os.path.join(BUILD_DIR, item)
        if item not in keep:
            if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
            elif item.endswith((".o", ".a", ".cmake")): os.remove(p)
    shutil.rmtree(SERVER_DIR, ignore_errors=True)


def test_db_connection(cfg):
    print("\n[=] Test de connexion à la base de données...")
    host     = cfg.get("Database", "mysql_host")
    port     = int(cfg.get("Database", "mysql_port", fallback="3306"))
    database = cfg.get("Database", "mysql_database")
    user     = cfg.get("Database", "mysql_username")
    password = cfg.get("Database", "mysql_password")
    print(f"[=] Connexion à {user}@{host}:{port}/{database}")
    try:
        conn = pymysql.connect(host=host, port=port, user=user,
                               password=password, database=database,
                               connect_timeout=10)
        conn.close()
        print("[✓] Connexion DB réussie !")
    except Exception as e:
        print(f"[!] ERREUR connexion DB : {e}")
        sys.exit(1)


def _cfg_get(cfg, section, option, fallback):
    try:
        return cfg.get(section, option)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return fallback


def write_config(cfg):
    """
    Écrit les fichiers .ini.

    Auth/Master/Chat : écoutent directement sur leurs ports publics.
    World            : écoute sur world_port_start=3000 (interne, jamais exposé).
                       Le proxy Python sur 25631 fait le relais + patch des paquets.
    """
    print("\n[=] Écriture de la configuration...")
    os.makedirs(BUILD_DIR, exist_ok=True)

    mysql_host = _cfg_get(cfg, "Database", "mysql_host",     "")
    mysql_port = _cfg_get(cfg, "Database", "mysql_port",     "3306")
    mysql_db   = _cfg_get(cfg, "Database", "mysql_database", "")
    mysql_user = _cfg_get(cfg, "Database", "mysql_username", "")
    mysql_pass = _cfg_get(cfg, "Database", "mysql_password", "")

    client_loc     = _cfg_get(cfg, "General", "client_location", "/home/container/client")
    use_custom_fdb = _cfg_get(cfg, "General", "use_custom_fdb",  "0")
    fdb_path       = _cfg_get(cfg, "General", "fdb_path",        "")

    external_ip = _cfg_get(cfg, "Networking", "external_ip",        "0.0.0.0")
    auth_port   = _cfg_get(cfg, "Networking", "auth_server_port",   DEFAULT_AUTH_PORT)
    world_port  = _cfg_get(cfg, "Networking", "world_server_port",  DEFAULT_WORLD_PORT)  # port public proxy
    chat_port   = _cfg_get(cfg, "Networking", "chat_server_port",   DEFAULT_CHAT_PORT)
    master_port = _cfg_get(cfg, "Networking", "master_server_port", DEFAULT_MASTER_PORT)

    max_offline_time       = _cfg_get(cfg, "Gameplay", "max_offline_time",       "0")
    kick_after_failed_auth = _cfg_get(cfg, "Gameplay", "kick_after_failed_auth", "1")
    allow_mythran_commands = _cfg_get(cfg, "Gameplay", "allow_mythran_commands", "0")
    disable_anti_cheat     = _cfg_get(cfg, "Gameplay", "disable_anti_cheat",     "0")
    chatbot_enabled        = _cfg_get(cfg, "Gameplay", "chatbot_enabled",        "0")
    log_activity           = _cfg_get(cfg, "Gameplay", "log_activity",           "0")

    log_level      = _cfg_get(cfg, "Logging", "log_level",      "2")
    log_to_console = _cfg_get(cfg, "Logging", "log_to_console", "1")
    log_to_file    = _cfg_get(cfg, "Logging", "log_to_file",    "0")

    print(f"[=] external_ip        = {external_ip}")
    print(f"[=] auth_server_port   = {auth_port} (public direct)")
    print(f"[=] master_server_port = {master_port} (public direct)")
    print(f"[=] chat_server_port   = {chat_port} (public direct)")
    print(f"[=] world_server_port  = {world_port} (public via proxy)")
    print(f"[=] world_port_start   = {WORLD_PORT_INTERNAL_START} (interne, proxy → {world_port})")

    if external_ip == "0.0.0.0":
        print("[!] ATTENTION : external_ip=0.0.0.0 — ajoutez EXTERNAL_IP dans Pterodactyl.")

    common = (
        f"[Database]\n"
        f"mysql_host={mysql_host}\n"
        f"mysql_port={mysql_port}\n"
        f"mysql_database={mysql_db}\n"
        f"mysql_username={mysql_user}\n"
        f"mysql_password={mysql_pass}\n\n"
        f"[General]\n"
        f"client_location={client_loc}\n"
        f"use_custom_fdb={use_custom_fdb}\n"
        f"fdb_path={fdb_path}\n\n"
        f"[Gameplay]\n"
        f"max_offline_time={max_offline_time}\n"
        f"kick_after_failed_auth={kick_after_failed_auth}\n"
        f"allow_mythran_commands={allow_mythran_commands}\n"
        f"disable_anti_cheat={disable_anti_cheat}\n"
        f"chatbot_enabled={chatbot_enabled}\n"
        f"log_activity={log_activity}\n\n"
        f"[Logging]\n"
        f"log_level={log_level}\n"
        f"log_to_console={log_to_console}\n"
        f"log_to_file={log_to_file}\n\n"
    )

    # Auth/Master/Chat voient world_server_port=25631 (port public du proxy)
    # world_port_start=3000 (internes, utilisé par MasterServer pour allouer les WorldServers)
    networking = (
        f"[Networking]\n"
        f"external_ip={external_ip}\n"
        f"listening_ip=0.0.0.0\n"
        f"auth_server_port={auth_port}\n"
        f"world_server_port={world_port}\n"
        f"world_port_start={WORLD_PORT_INTERNAL_START}\n"
        f"chat_server_port={chat_port}\n"
        f"master_server_port={master_port}\n"
    )

    for filename in ["authconfig.ini", "masterconfig.ini", "worldconfig.ini", "chatconfig.ini"]:
        with open(os.path.join(BUILD_DIR, filename), "w") as f:
            f.write(common + networking)

    print("[✓] Configs écrites.")
    return int(world_port), external_ip


def check_client_files(cfg):
    print("\n[=] Vérification des fichiers client...")
    client_path = _cfg_get(cfg, "General", "client_location", "/home/container/client")
    if not os.path.isdir(client_path):
        print(f"[!] ERREUR : client introuvable à {client_path}")
        sys.exit(1)
    missing = [f for f in ["res/cdclient.fdb", "locale/locale.xml"]
               if not os.path.isfile(os.path.join(client_path, f))]
    if missing:
        print(f"[!] Fichiers client manquants : {', '.join(missing)}")
        sys.exit(1)
    print(f"[✓] Fichiers client OK à {client_path}")


def start_server(public_world_port: int, public_ip: str):
    print("\n[=] Démarrage de Darkflame Universe...")
    master = find_binary("master")
    if master is None:
        print(f"[!] ERREUR : MasterServer introuvable dans {BUILD_DIR}")
        sys.exit(1)
    if needs_glibc_compat():
        setup_glibc_compat()
    if os.path.isdir(GLIBC_DIR):
        setup_ld_library_path()

    print("\n[=] Démarrage du proxy UDP World...")
    start_world_proxy(public_world_port, public_ip)
    time.sleep(0.5)

    print(f"[✓] Lancement de {master}")
    os.chdir(BUILD_DIR)
    os.execve(master, [master], os.environ)


def main():
    if "--add-account" in sys.argv:
        idx = sys.argv.index("--add-account")
        cfg = load_config()
        test_db_connection(cfg)
        cmd_add_account(cfg, sys.argv[idx + 1:])
        return

    print("===================================================")
    print(" Darkflame Universe - Pterodactyl Python Container")
    print("===================================================")
    cfg = load_config()
    if os.path.isfile(BINS_ZIP):
        install_runtime_deps()
        extract_prebuilt()
    else:
        print("[!] Aucun darkflame-bins.zip trouvé → tentative de compilation")
        install_build_deps()
        clone_server()
        build_server()
    check_client_files(cfg)
    test_db_connection(cfg)
    pub_world, public_ip = write_config(cfg)
    setup_server_data(cfg)
    setup_first_account(cfg)
    start_server(pub_world, public_ip)


if __name__ == "__main__":
    main()
