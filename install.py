#!/usr/bin/env python3
"""
Darkflame Universe - Pterodactyl Python Container

Architecture réseau :
  - Auth/Master/Chat écoutent directement sur leurs ports publics
  - WorldServers écoutent sur localhost:25631, localhost:25632, localhost:25633...
    (world_port_start=25631, DFU incrémente pour chaque instance)
  - Proxy UDP Python écoute sur 0.0.0.0:25631 (le seul port public world)
    * Il maintient la table session client_addr -> port_interne
    * Il patche tous les paquets sortants qui contiennent 127.0.0.1:25631+
      pour les remplacer par external_ip:25631
    * Ainsi le client reçoit toujours "connecte-toi sur IP:25631"
      et le proxy route vers le bon WorldServer interne
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
import re

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
DEFAULT_WORLD_PORT  = "25631"

# Plage de ports internes world (world_port_start + 0..N)
# DFU démarre sur world_port_start, puis +1, +2... pour chaque instance
WORLD_PORT_BASE  = 25631
WORLD_PORT_COUNT = 20   # supporte jusqu'à 20 instances world simultanées


# ---------------------------------------------------------------------------
# Proxy UDP World
#
# Problème : DFU fait écouter chaque WorldServer sur localhost:25631,
# localhost:25632, localhost:25633... Ces ports sont inaccessibles de l'extérieur.
# Quand le client doit changer de zone, Auth/World lui envoie un paquet
# contenant "127.0.0.1:2563X" → client ne peut pas s'y connecter.
#
# Solution :
#   1) Le proxy écoute sur 0.0.0.0:25631 (seul port world public)
#   2) À la première connexion d'un client, il le route vers localhost:25631
#      (char select)
#   3) Quand le WorldServer envoie un paquet avec une IP/port interne
#      (127.0.0.1:2563X ou localhost:2563X), le proxy le patche pour
#      mettre external_ip:25631 à la place
#   4) Le client se reconnecte sur 25631 → le proxy détecte que c'est
#      une nouvelle session et la route vers le bon WorldServer interne
#      (celui qui vient d'être créé pour cette zone)
# ---------------------------------------------------------------------------

def patch_redirect_packet(data: bytes, world_ports: set,
                          public_ip: str, public_port: int) -> tuple:
    """
    Patche un paquet UDP sortant (world->client).
    Cherche le pattern [len_u8][ip_ascii][port_u16_LE] où port est dans world_ports.
    Remplace IP+port par public_ip:public_port.
    Retourne (patched_data, nouveau_port_interne_ou_None).
    """
    result = bytearray(data)
    i = 0
    found_port = None
    patched = False

    while i < len(result) - 3:
        ip_len = result[i]
        if 7 <= ip_len <= 15:
            end_ip = i + 1 + ip_len
            if end_ip + 2 <= len(result):
                try:
                    candidate = result[i+1:end_ip].decode('ascii')
                except Exception:
                    i += 1
                    continue
                parts = candidate.split('.')
                if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
                    port_val = struct.unpack_from('<H', bytes(result[end_ip:end_ip+2]))[0]
                    if port_val in world_ports:
                        found_port = port_val
                        new_ip_b = public_ip.encode('ascii')
                        repl = bytes([len(new_ip_b)]) + new_ip_b + struct.pack('<H', public_port)
                        result = result[:i] + bytearray(repl) + result[end_ip+2:]
                        i += len(repl)
                        patched = True
                        print(f"[proxy] Patch redirect {candidate}:{port_val} → {public_ip}:{public_port}")
                        continue
        i += 1

    return bytes(result), found_port if patched else None


class WorldProxy:
    """
    Proxy UDP single-port pour DFU.

    - Écoute sur 0.0.0.0:PUBLIC_PORT
    - Route chaque client vers un WorldServer interne
    - Patche les paquets de redirect de zone
    - Quand un redirect est détecté vers un nouveau port interne X,
      la prochaine connexion du même client (ou nouvelle IP) sera
      routée vers X
    """
    SESSION_TIMEOUT = 180

    def __init__(self, public_port: int, public_ip: str,
                 world_port_base: int, world_port_count: int):
        self.public_port  = public_port
        self.public_ip    = public_ip
        self.base         = world_port_base
        self.count        = world_port_count
        self.world_ports  = set(range(world_port_base, world_port_base + world_port_count))

        self.sock      = None
        # client_addr -> internal_port
        self.sessions  = {}
        self.last_seen = {}
        # pending redirects: quand on patche un paquet, on note le nouveau
        # port interne pour que la prochaine connexion soit routée là
        self.pending_redirect = {}  # client_addr -> internal_port
        self.lock = threading.Lock()
        # sockets vers chaque port interne
        self.bsocks = {}

    def _backend_sock(self, port: int) -> socket.socket:
        if port not in self.bsocks:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            self.bsocks[port] = s
        return self.bsocks[port]

    def _active_world_ports(self) -> set:
        """Lit /proc/net/udp pour trouver les WorldServers actifs."""
        active = set()
        try:
            with open('/proc/net/udp') as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    try:
                        p = int(parts[1].split(':')[1], 16)
                        if p in self.world_ports:
                            active.add(p)
                    except Exception:
                        pass
        except Exception:
            pass
        return active

    def _reader_thread(self, port: int):
        """Thread dédié à la lecture des paquets du WorldServer sur `port`."""
        sock = self._backend_sock(port)
        while True:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[proxy] reader :{port} erreur: {e}")
                time.sleep(0.5)
                continue

            # Patche les redirections de zone dans le paquet
            data, redirect_port = patch_redirect_packet(
                data, self.world_ports, self.public_ip, self.public_port
            )

            with self.lock:
                clients = [c for c, p in self.sessions.items() if p == port]
                if redirect_port and redirect_port != port:
                    for c in clients:
                        self.pending_redirect[c] = redirect_port
                        print(f"[proxy] Redirect prévu pour {c} → :{redirect_port}")

            for addr in clients:
                try:
                    self.sock.sendto(data, addr)
                except Exception:
                    pass

    def _cleanup_thread(self):
        while True:
            time.sleep(30)
            now = time.time()
            with self.lock:
                dead = [c for c, t in self.last_seen.items()
                        if now - t > self.SESSION_TIMEOUT]
                for c in dead:
                    self.sessions.pop(c, None)
                    self.last_seen.pop(c, None)
                    self.pending_redirect.pop(c, None)

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", self.public_port))
        print(f"[proxy] Écoute 0.0.0.0:{self.public_port}")
        print(f"[proxy] Ports world internes : {self.base}..{self.base + self.count - 1}")

        for p in range(self.base, self.base + self.count):
            threading.Thread(target=self._reader_thread, args=(p,), daemon=True).start()

        threading.Thread(target=self._cleanup_thread, daemon=True).start()

        while True:
            try:
                data, client_addr = self.sock.recvfrom(65535)
            except Exception:
                continue

            now = time.time()
            with self.lock:
                self.last_seen[client_addr] = now

                if client_addr in self.pending_redirect:
                    # Le client revient après une redirection de zone
                    target = self.pending_redirect.pop(client_addr)
                    self.sessions[client_addr] = target
                    print(f"[proxy] Session {client_addr} redirigée → :{target}")
                elif client_addr not in self.sessions:
                    # Nouvelle connexion → char select (port de base)
                    active = self._active_world_ports()
                    target = self.base if not active else min(active)
                    self.sessions[client_addr] = target
                    print(f"[proxy] Nouvelle session {client_addr} → :{target}")
                else:
                    target = self.sessions[client_addr]

            try:
                self._backend_sock(target).sendto(data, ("127.0.0.1", target))
            except Exception as e:
                print(f"[proxy] Erreur envoi :{target}: {e}")


def start_world_proxy(public_port: int, public_ip: str) -> WorldProxy:
    proxy = WorldProxy(public_port, public_ip, WORLD_PORT_BASE, WORLD_PORT_COUNT)
    t = threading.Thread(target=proxy.start, daemon=True)
    t.start()
    time.sleep(0.2)
    print(f"[proxy] Démarré sur :{public_port}")
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

    if not db_tables_ready(cfg):
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
        print(f"[✓] {count} compte(s) existant(s) en DB, skip.")
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
        print("[!] ERREUR : tables DB inexistantes.")
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
        try:
            src = t.extractfile(m)
            if src:
                with open(os.path.join(out_dir, base), 'wb') as f:
                    shutil.copyfileobj(src, f)
                os.chmod(os.path.join(out_dir, base), 0o755)
        except Exception as e:
            print(f"  [!] {base}: {e}")
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
        except Exception:
            pass


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
    data_tar = None
    if r.returncode != 0:
        data_tar = _extract_deb_ar(deb_path, work)
    else:
        data_tar = next((os.path.join(work, f) for f in os.listdir(work)
                         if f.startswith("data.tar")), None)
    if data_tar is None:
        shutil.rmtree(work, ignore_errors=True)
        return
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
                tgt = os.path.join(glibc_dir, os.path.basename(os.readlink(p)))
                if os.path.isfile(tgt):
                    os.chmod(tgt, 0o755)
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
        print("[✓] GLIBC compat déjà appliqué.")
        return
    if not os.path.isdir(GLIBC_DIR) or not any('libc' in f for f in os.listdir(GLIBC_DIR)):
        print("[=] Téléchargement libs GLIBC 2.39...")
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
                print(f"[!] {url.split('/')[-1]}: {e}")
        shutil.rmtree(tmp, ignore_errors=True)
    if not any('libc' in f for f in os.listdir(GLIBC_DIR)):
        print("[!] libc.so.6 introuvable")
        sys.exit(1)
    if not os.path.isfile(PATCHELF):
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
        print("[!] ld-linux introuvable")
        sys.exit(1)
    mariadb_lib = os.path.join(BUILD_DIR, "thirdparty", "mariadb-connector-cpp",
                               "src", "mariadb_connector_cpp-build")
    rpath = f"{GLIBC_DIR}:{BUILD_DIR}:{mariadb_lib}"
    patched = 0
    for key in BINARY_NAMES:
        b = find_binary(key)
        if b and run(f"{PATCHELF} --set-interpreter {ld} --set-rpath {rpath} {b}", check=False).returncode == 0:
            patched += 1
    if patched:
        open(PATCHED_FLAG, 'w').close()
        print(f"[✓] {patched} binaire(s) patchés GLIBC.")


def setup_ld_library_path():
    paths = [BUILD_DIR, GLIBC_DIR]
    for root, dirs, files in os.walk(BUILD_DIR):
        for f in files:
            if f.endswith(".so") or ".so." in f:
                paths.append(root)
                break
    cur = os.environ.get("LD_LIBRARY_PATH", "")
    os.environ["LD_LIBRARY_PATH"] = ":".join(dict.fromkeys(paths)) + (":" + cur if cur else "")


def extract_prebuilt():
    print("\n[=] darkflame-bins.zip → extraction binaires")
    if os.path.isfile(EXTRACT_FLAG) and find_binary("master") is not None:
        print("[✓] Binaires déjà extraits.")
        return
    if os.path.isdir(BUILD_DIR):
        shutil.rmtree(BUILD_DIR, ignore_errors=True)
    if os.path.isfile(PATCHED_FLAG):
        os.remove(PATCHED_FLAG)
    os.makedirs(BUILD_DIR, exist_ok=True)
    with zipfile.ZipFile(BINS_ZIP, 'r') as z:
        z.extractall(BUILD_DIR)
    for key in BINARY_NAMES:
        for name in BINARY_NAMES[key]:
            p = os.path.join(BUILD_DIR, name)
            if os.path.isfile(p):
                os.chmod(p, 0o755)
    open(EXTRACT_FLAG, 'w').close()
    print("[✓] Binaires extraits.")


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
    missing = [(s, d) for s, d in DFS_PLAIN_DIRS
               if not os.path.isdir(os.path.join(BUILD_DIR, d))]
    nav_dir = os.path.join(BUILD_DIR, "navmeshes")
    need_nav = not (os.path.isdir(nav_dir) and
                    any(f.endswith(".bin") for f in os.listdir(nav_dir)))
    if not missing and not need_nav:
        print("[✓] Ressources repo déjà présentes.")
        return
    tar_path = os.path.join(HOME_DIR, "dfs-main.tar.gz")
    download_file(DFS_TARBALL_URL, tar_path)
    with tarfile.open(tar_path, 'r:gz') as t:
        members = t.getmembers()
        root = ""
        for m in members:
            parts = m.name.split('/')
            if len(parts) >= 2:
                root = parts[0] + "/"
                break
        for src_name, dst_name in missing:
            cnt = _extract_dir_from_tar(t, members, root + src_name + "/",
                                         os.path.join(BUILD_DIR, dst_name))
            print(f"[✓] {dst_name}/ : {cnt} fichier(s)")
        if need_nav:
            nav_zip = root + "resources/navmeshes.zip"
            nav_m = next((m for m in members if m.name == nav_zip), None)
            if nav_m:
                data = t.extractfile(nav_m).read()
                os.makedirs(nav_dir, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for name in z.namelist():
                        fname = os.path.basename(name)
                        if fname.endswith(".bin"):
                            with z.open(name) as src, open(os.path.join(nav_dir, fname), 'wb') as out:
                                shutil.copyfileobj(src, out)
                print(f"[✓] navmeshes/ OK")
    os.remove(tar_path)


def setup_server_data(cfg):
    client_path = cfg.get("General", "client_location", fallback="/home/container/client")
    for sub in ["res", "locale"]:
        src = os.path.join(client_path, sub)
        dst = os.path.join(BUILD_DIR, sub)
        if not os.path.isdir(src):
            print(f"[!] ERREUR : {src} introuvable !")
            sys.exit(1)
        if not os.path.exists(dst):
            print(f"[=] Copie {sub}/...")
            shutil.copytree(src, dst)
        else:
            print(f"[✓] {sub}/ déjà présent.")
    fetch_repo_resources()
    os.makedirs(os.path.join(BUILD_DIR, "logs"), exist_ok=True)


def install_runtime_deps():
    r = subprocess.run("apt-get update -qq", shell=True, capture_output=True)
    if r.returncode == 0:
        subprocess.run("apt-get install -y libmariadb3 libssl3 unzip binutils", shell=True)


def install_build_deps():
    if subprocess.run("apt-get update -qq", shell=True, capture_output=True).returncode != 0:
        print("[!] apt-get non disponible.")
        sys.exit(1)
    run("apt-get install -y git cmake g++ zlib1g-dev libssl-dev libmariadb-dev-compat libmariadb-dev unzip")


def clone_server():
    if os.path.exists(SERVER_DIR):
        run("git pull", cwd=SERVER_DIR)
    else:
        run(f"git clone --recursive https://github.com/DarkflameUniverse/DarkflameServer.git {SERVER_DIR}")


def build_server():
    if find_binary("master") is not None:
        print("[✓] Déjà compilé.")
        return
    os.makedirs(BUILD_DIR, exist_ok=True)
    run("cmake .. -DCMAKE_BUILD_TYPE=Release", cwd=BUILD_DIR)
    run("make -j$(nproc)", cwd=BUILD_DIR)
    keep = set(sum(BINARY_NAMES.values(), []))
    for item in os.listdir(BUILD_DIR):
        p = os.path.join(BUILD_DIR, item)
        if item not in keep:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif item.endswith((".o", ".a", ".cmake")):
                os.remove(p)
    shutil.rmtree(SERVER_DIR, ignore_errors=True)


def test_db_connection(cfg):
    print("\n[=] Test connexion DB...")
    host = cfg.get("Database", "mysql_host")
    port = int(cfg.get("Database", "mysql_port", fallback="3306"))
    db   = cfg.get("Database", "mysql_database")
    user = cfg.get("Database", "mysql_username")
    pw   = cfg.get("Database", "mysql_password")
    try:
        pymysql.connect(host=host, port=port, user=user,
                        password=pw, database=db, connect_timeout=10).close()
        print("[✓] Connexion DB OK")
    except Exception as e:
        print(f"[!] ERREUR DB : {e}")
        sys.exit(1)


def _cfg_get(cfg, section, option, fallback):
    try:
        return cfg.get(section, option)
    except (configparser.NoSectionError, configparser.NoOptionError):
        return fallback


def write_config(cfg):
    """
    world_port_start = 25631 (DFU alloue 25631, 25632, 25633... sur localhost)
    world_server_port = 25631 (port public, via proxy)
    Le proxy intercepte et patche les redirections internes → IP:25631
    """
    print("\n[=] Écriture configuration...")
    os.makedirs(BUILD_DIR, exist_ok=True)

    mysql_host = _cfg_get(cfg, "Database", "mysql_host",     "")
    mysql_port = _cfg_get(cfg, "Database", "mysql_port",     "3306")
    mysql_db   = _cfg_get(cfg, "Database", "mysql_database", "")
    mysql_user = _cfg_get(cfg, "Database", "mysql_username", "")
    mysql_pass = _cfg_get(cfg, "Database", "mysql_password", "")
    client_loc = _cfg_get(cfg, "General", "client_location", "/home/container/client")
    use_fdb    = _cfg_get(cfg, "General", "use_custom_fdb",  "0")
    fdb_path   = _cfg_get(cfg, "General", "fdb_path",        "")

    external_ip = _cfg_get(cfg, "Networking", "external_ip",        "0.0.0.0")
    auth_port   = _cfg_get(cfg, "Networking", "auth_server_port",   DEFAULT_AUTH_PORT)
    world_port  = _cfg_get(cfg, "Networking", "world_server_port",  DEFAULT_WORLD_PORT)
    chat_port   = _cfg_get(cfg, "Networking", "chat_server_port",   DEFAULT_CHAT_PORT)
    master_port = _cfg_get(cfg, "Networking", "master_server_port", DEFAULT_MASTER_PORT)

    max_offline  = _cfg_get(cfg, "Gameplay", "max_offline_time",       "0")
    kick_auth    = _cfg_get(cfg, "Gameplay", "kick_after_failed_auth", "1")
    mythran      = _cfg_get(cfg, "Gameplay", "allow_mythran_commands", "0")
    anti_cheat   = _cfg_get(cfg, "Gameplay", "disable_anti_cheat",     "0")
    chatbot      = _cfg_get(cfg, "Gameplay", "chatbot_enabled",        "0")
    log_act      = _cfg_get(cfg, "Gameplay", "log_activity",           "0")
    log_level    = _cfg_get(cfg, "Logging",  "log_level",              "2")
    log_console  = _cfg_get(cfg, "Logging",  "log_to_console",         "1")
    log_file     = _cfg_get(cfg, "Logging",  "log_to_file",            "0")

    if external_ip == "0.0.0.0":
        print("[!] ATTENTION : external_ip=0.0.0.0 — définissez EXTERNAL_IP dans Pterodactyl !")

    print(f"[=] auth={auth_port} master={master_port} chat={chat_port} world={world_port} (proxy)")
    print(f"[=] world_port_start={world_port} → instances internes sur localhost:{world_port},{int(world_port)+1}...")

    content = (
        f"[Database]\n"
        f"mysql_host={mysql_host}\nmysql_port={mysql_port}\n"
        f"mysql_database={mysql_db}\nmysql_username={mysql_user}\nmysql_password={mysql_pass}\n\n"
        f"[General]\n"
        f"client_location={client_loc}\nuse_custom_fdb={use_fdb}\nfdb_path={fdb_path}\n\n"
        f"[Gameplay]\n"
        f"max_offline_time={max_offline}\nkick_after_failed_auth={kick_auth}\n"
        f"allow_mythran_commands={mythran}\ndisable_anti_cheat={anti_cheat}\n"
        f"chatbot_enabled={chatbot}\nlog_activity={log_act}\n\n"
        f"[Logging]\n"
        f"log_level={log_level}\nlog_to_console={log_console}\nlog_to_file={log_file}\n\n"
        f"[Networking]\n"
        f"external_ip={external_ip}\nlistening_ip=0.0.0.0\n"
        f"auth_server_port={auth_port}\n"
        f"world_server_port={world_port}\n"
        f"world_port_start={world_port}\n"   # DFU alloue depuis ici sur localhost
        f"chat_server_port={chat_port}\n"
        f"master_server_port={master_port}\n"
    )

    for fn in ["authconfig.ini", "masterconfig.ini", "worldconfig.ini", "chatconfig.ini"]:
        with open(os.path.join(BUILD_DIR, fn), "w") as f:
            f.write(content)

    print("[✓] Configs écrites.")
    return int(world_port), external_ip


def check_client_files(cfg):
    client_path = _cfg_get(cfg, "General", "client_location", "/home/container/client")
    if not os.path.isdir(client_path):
        print(f"[!] Client introuvable : {client_path}")
        sys.exit(1)
    missing = [f for f in ["res/cdclient.fdb", "locale/locale.xml"]
               if not os.path.isfile(os.path.join(client_path, f))]
    if missing:
        print(f"[!] Fichiers client manquants : {', '.join(missing)}")
        sys.exit(1)
    print(f"[✓] Client OK")


def start_server(public_world_port: int, public_ip: str):
    print("\n[=] Démarrage Darkflame Universe...")
    master = find_binary("master")
    if master is None:
        print(f"[!] MasterServer introuvable dans {BUILD_DIR}")
        sys.exit(1)
    if needs_glibc_compat():
        setup_glibc_compat()
    if os.path.isdir(GLIBC_DIR):
        setup_ld_library_path()

    # Démarrer le proxy AVANT MasterServer pour être prêt dès le départ
    print("[=] Démarrage proxy UDP world...")
    start_world_proxy(public_world_port, public_ip)
    time.sleep(0.5)

    print(f"[✓] Lancement {master}")
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
        print("[!] Pas de darkflame-bins.zip → compilation")
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
