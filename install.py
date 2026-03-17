#!/usr/bin/env python3
"""
Darkflame Universe - Script d'installation et de démarrage
pour container Python Pterodactyl avec DB externe
"""

import os
import re
import subprocess
import sys
import shutil
import zipfile
import configparser
import urllib.request
import tarfile
import io

def pip_install(pkg):
    subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=True)

try:
    import pymysql
except ImportError:
    print("[=] Installation de pymysql...")
    pip_install("pymysql")
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
GLIBC_DIR    = os.path.join(HOME_DIR, "glibc-compat")
PATCHELF     = os.path.join(HOME_DIR, "patchelf")
PATCHED_FLAG = os.path.join(HOME_DIR, ".glibc_patched")
EXTRACT_FLAG = os.path.join(HOME_DIR, ".bins_extracted")

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


def load_config():
    if not os.path.isfile(CONFIG_FILE):
        print(f"[!] ERREUR : {CONFIG_FILE} introuvable !")
        sys.exit(1)

    env_map = {
        "MYSQL_HOST":         ("Database",   "mysql_host"),
        "MYSQL_PORT":         ("Database",   "mysql_port"),
        "MYSQL_DATABASE":     ("Database",   "mysql_database"),
        "MYSQL_USER":         ("Database",   "mysql_username"),
        "MYSQL_PASSWORD":     ("Database",   "mysql_password"),
        "CLIENT_PATH":        ("General",    "client_location"),
        "EXTERNAL_IP":        ("Networking", "external_ip"),
        "AUTH_SERVER_PORT":   ("Networking", "auth_server_port"),
        "WORLD_SERVER_PORT":  ("Networking", "world_server_port"),
        "CHAT_SERVER_PORT":   ("Networking", "chat_server_port"),
        "MASTER_SERVER_PORT": ("Networking", "master_server_port"),
    }

    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)

    for env_key, (section, option) in env_map.items():
        val = os.environ.get(env_key)
        if val:
            if not cfg.has_section(section):
                cfg.add_section(section)
            cfg.set(section, option, val)
            print(f"[=] Env override: {section}.{option} = {val}")

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
        print("[!] ldd a échoué → activation GLIBC compat par précaution")
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
        is_so      = ".so" in base
        is_ld_real = base.startswith("ld-") and base.endswith(".so")
        if not (is_so or is_ld_real):
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
        if link_target == base:
            continue
        if not os.path.isfile(target_path):
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
                print(f"[✓] ld réel trouvé : {f}")
                return p
    for f in os.listdir(glibc_dir):
        if "ld-linux" in f:
            p = os.path.join(glibc_dir, f)
            if os.path.islink(p):
                target_name = os.path.basename(os.readlink(p))
                if target_name == f:
                    continue
                real = os.path.join(glibc_dir, target_name)
                if os.path.isfile(real):
                    os.chmod(real, 0o755)
                    print(f"[✓] ld via symlink : {f} -> {target_name}")
                    return p
            elif os.path.isfile(p):
                os.chmod(p, 0o755)
                print(f"[✓] ld-linux fichier direct : {f}")
                return p
    for f in os.listdir(glibc_dir):
        if f.startswith("ld-"):
            p = os.path.join(glibc_dir, f)
            resolved = os.path.realpath(p)
            if os.path.isfile(resolved):
                os.chmod(resolved, 0o755)
                return resolved
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

    libs = os.listdir(GLIBC_DIR)
    if not any('libc' in f for f in libs):
        print("[!] ERREUR : libc.so.6 non trouvé")
        sys.exit(1)

    if not os.path.isfile(PATCHELF):
        print("[=] Téléchargement patchelf...")
        tar = os.path.join(HOME_DIR, "patchelf.tar.gz")
        download_file(PATCHELF_URL, tar)
        with tarfile.open(tar) as t:
            for m in t.getmembers():
                if m.name.endswith("patchelf") and m.isfile():
                    src = t.extractfile(m)
                    with open(PATCHELF, 'wb') as out_f:
                        shutil.copyfileobj(src, out_f)
                    break
        os.chmod(PATCHELF, 0o755)
        os.remove(tar)

    ld = find_ld(GLIBC_DIR)
    if ld is None:
        print(f"[!] ld-linux introuvable.")
        sys.exit(1)
    print(f"[✓] ld-linux : {ld}")

    mariadb_lib = os.path.join(BUILD_DIR, "thirdparty", "mariadb-connector-cpp",
                               "src", "mariadb_connector_cpp-build")
    rpath = f"{GLIBC_DIR}:{BUILD_DIR}:{mariadb_lib}"

    patched_count = 0
    for key in BINARY_NAMES:
        b = find_binary(key)
        if b:
            print(f"[=] Patch {os.path.basename(b)} → GLIBC compat...")
            result = run(f"{PATCHELF} --set-interpreter {ld} --set-rpath {rpath} {b}", check=False)
            if result.returncode == 0:
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
        print("[✓] Tous les dossiers repo déjà présents (migrations, vanity, navmeshes), skip.")
        return

    tar_path = os.path.join(HOME_DIR, "dfs-main.tar.gz")
    print("[=] Téléchargement DarkflameServer (tarball)...")
    try:
        download_file(DFS_TARBALL_URL, tar_path)
    except Exception as e:
        print(f"[!] Échec téléchargement tarball DarkflameServer : {e}")
        sys.exit(1)

    print("[=] Extraction depuis le tarball...")
    with tarfile.open(tar_path, 'r:gz') as t:
        members = t.getmembers()
        root_prefix = ""
        for m in members:
            parts = m.name.split('/')
            if len(parts) >= 2:
                root_prefix = parts[0] + "/"
                break

        for src_name, dst_name in missing_dirs:
            src_prefix = root_prefix + src_name + "/"
            dest_dir   = os.path.join(BUILD_DIR, dst_name)
            count = _extract_dir_from_tar(t, members, src_prefix, dest_dir)
            print(f"[✓] {dst_name}/ : {count} fichier(s) extrait(s)")

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
                bin_count = len([f for f in os.listdir(nav_dir) if f.endswith(".bin")])
                print(f"[✓] navmeshes/ : {bin_count} fichier(s) .bin extrait(s)")
            else:
                print("[!] resources/navmeshes.zip introuvable dans le tarball")

    os.remove(tar_path)
    print("[✓] Ressources DarkflameServer OK.")


def setup_server_data(cfg):
    print("\n[=] Vérification des données serveur...")
    client_path = cfg.get("General", "client_location", fallback="/home/container/client")

    client_copies = [
        (os.path.join(client_path, "res"),    os.path.join(BUILD_DIR, "res")),
        (os.path.join(client_path, "locale"), os.path.join(BUILD_DIR, "locale")),
    ]
    for src, dst in client_copies:
        if os.path.isdir(src):
            if os.path.exists(dst):
                print(f"[✓] {os.path.basename(dst)}/ déjà présent, skip.")
            else:
                print(f"[=] Copie {os.path.basename(src)}/ → {dst} ...")
                shutil.copytree(src, dst)
                print(f"[✓] {os.path.basename(src)}/ copié.")
        else:
            print(f"[!] ERREUR : {src} introuvable dans le client !")
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
    print("\n[=] Écriture de la configuration...")
    os.makedirs(BUILD_DIR, exist_ok=True)

    external_ip   = _cfg_get(cfg, "Networking", "external_ip",        "0.0.0.0")
    auth_port     = _cfg_get(cfg, "Networking", "auth_server_port",   "25896")
    world_port    = _cfg_get(cfg, "Networking", "world_server_port",  "25740")
    chat_port     = _cfg_get(cfg, "Networking", "chat_server_port",   "25784")
    master_port   = _cfg_get(cfg, "Networking", "master_server_port", "25846")
    mysql_host    = _cfg_get(cfg, "Database",   "mysql_host",         "")
    mysql_port    = _cfg_get(cfg, "Database",   "mysql_port",         "3306")
    mysql_db      = _cfg_get(cfg, "Database",   "mysql_database",     "")
    mysql_user    = _cfg_get(cfg, "Database",   "mysql_username",     "")
    mysql_pass    = _cfg_get(cfg, "Database",   "mysql_password",     "")
    client_loc    = _cfg_get(cfg, "General",    "client_location",    "/home/container/client")

    print(f"[=] external_ip       = {external_ip}")
    print(f"[=] auth_server_port  = {auth_port}")
    print(f"[=] world_server_port = {world_port}")
    print(f"[=] chat_server_port  = {chat_port}")
    print(f"[=] master_server_port= {master_port}")

    if external_ip == "0.0.0.0":
        print("[!] ATTENTION : external_ip=0.0.0.0 — les clients ne pourront pas se connecter !")
        print("[!] Ajoutez la variable d'env EXTERNAL_IP=<votre_ip_publique> dans FeatherPanel.")

    common = (
        f"[Database]\n"
        f"mysql_host={mysql_host}\n"
        f"mysql_port={mysql_port}\n"
        f"mysql_database={mysql_db}\n"
        f"mysql_username={mysql_user}\n"
        f"mysql_password={mysql_pass}\n"
        f"\n"
        f"[General]\n"
        f"client_location={client_loc}\n"
        f"\n"
        f"[Logging]\n"
        f"log_level=2\n"
        f"log_to_console=1\n"
        f"\n"
    )

    networking_base = (
        f"external_ip={external_ip}\n"
        f"auth_server_port={auth_port}\n"
        f"world_server_port={world_port}\n"
        f"chat_server_port={chat_port}\n"
        f"master_server_port={master_port}\n"
    )

    configs = {
        "masterconfig.ini": common + "[Networking]\n" + networking_base,
        "authconfig.ini":   common + "[Networking]\n" + networking_base,
        "worldconfig.ini":  common + "[Networking]\n" + networking_base,
        "chatconfig.ini":   common + "[Networking]\n" + networking_base,
    }

    for filename, content in configs.items():
        dest = os.path.join(BUILD_DIR, filename)
        with open(dest, "w") as f:
            f.write(content)

    print("[✓] Configs écrites (authconfig, masterconfig, worldconfig, chatconfig).")


def check_client_files(cfg):
    print("\n[=] Vérification des fichiers client...")
    client_path = _cfg_get(cfg, "General", "client_location", "/home/container/client")
    if not os.path.isdir(client_path):
        print(f"[!] ERREUR : client introuvable à {client_path}")
        sys.exit(1)
    required = ["res/cdclient.fdb", "locale/locale.xml"]
    missing = [f for f in required if not os.path.isfile(os.path.join(client_path, f))]
    if missing:
        print(f"[!] Fichiers client manquants : {', '.join(missing)}")
        sys.exit(1)
    print(f"[✓] Fichiers client OK à {client_path}")


def start_server():
    print("\n[=] Démarrage de Darkflame Universe...")
    master = find_binary("master")
    if master is None:
        print(f"[!] ERREUR : MasterServer introuvable dans {BUILD_DIR}")
        sys.exit(1)
    if needs_glibc_compat():
        setup_glibc_compat()
    if os.path.isdir(GLIBC_DIR):
        setup_ld_library_path()
    print(f"[✓] Lancement de {master}")
    os.chdir(BUILD_DIR)
    os.execve(master, [master], os.environ)


def main():
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
    write_config(cfg)
    setup_server_data(cfg)
    start_server()


if __name__ == "__main__":
    main()
