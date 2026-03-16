#!/usr/bin/env python3
"""
Darkflame Universe - Script d'installation et de démarrage
pour container Python Pterodactyl avec DB externe
"""

import os
import subprocess
import sys
import shutil
import zipfile
import configparser
import urllib.request
import tarfile
import stat

# Auto-install pymysql et redémarre si absent
try:
    import pymysql
except ImportError:
    print("[=] Installation de pymysql...")
    subprocess.run([sys.executable, "-m", "pip", "install", "pymysql", "-q"], check=True)
    print("[=] Redémarrage du script...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

HOME_DIR    = "/home/container"
BINS_ZIP    = os.path.join(HOME_DIR, "darkflame-bins.zip")
BUILD_DIR   = os.path.join(HOME_DIR, "darkflame-build")
SERVER_DIR  = os.path.join(HOME_DIR, "DarkflameServer")
CONFIG_FILE = os.path.join(HOME_DIR, "config_template.ini")
GLIBC_DIR   = os.path.join(HOME_DIR, "glibc-compat")

# URL d'un GLIBC 2.38 portable (prebuilt, pas de compilation)
GLIBC_URL = "https://github.com/wheybags/glibc_version_header/releases/download/2.38/glibc-2.38-linux-x86_64.tar.gz"
# Fallback : utiliser les libs d'un container Ubuntu 24.04 via un tar prépackagé
GLIBC_LIBS_URL = "https://github.com/theo7791l/darkflame-python/releases/download/glibc-compat/glibc-2.38-libs.tar.gz"

BINARY_NAMES = {
    "master": ["MasterServer", "masterserver"],
    "auth":   ["AuthServer",   "authserver"],
    "chat":   ["ChatServer",   "chatserver"],
    "world":  ["WorldServer",  "worldserver"],
}

def load_config():
    if not os.path.isfile(CONFIG_FILE):
        print(f"[!] ERREUR : {CONFIG_FILE} introuvable !")
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_FILE)
    return cfg

def find_binary(name_key):
    for name in BINARY_NAMES[name_key]:
        path = os.path.join(BUILD_DIR, name)
        if os.path.isfile(path):
            return path
    return None

def run(cmd, cwd=None, check=True):
    print(f"[+] {cmd}")
    return subprocess.run(cmd, shell=True, cwd=cwd, check=check)

def has_sudo():
    return subprocess.run("sudo -n true", shell=True, capture_output=True).returncode == 0

def needs_glibc_compat():
    """Vérifie si les binaires nécessitent GLIBC_2.38 non dispo dans le container."""
    master = find_binary("master")
    if master is None:
        return False
    r = subprocess.run(["ldd", master], capture_output=True, text=True)
    return "GLIBC_2.38" in r.stdout or "GLIBC_2.38" in r.stderr or "not found" in r.stdout

def setup_glibc_compat():
    """Télécharge les libs GLIBC 2.38 portables et patche les binaires avec patchelf."""
    if os.path.isdir(GLIBC_DIR) and os.listdir(GLIBC_DIR):
        print("[✓] GLIBC compat déjà présent, skip.")
        return

    print("[=] GLIBC_2.38 requis mais absent → téléchargement des libs compatibles...")
    os.makedirs(GLIBC_DIR, exist_ok=True)

    # Télécharger patchelf
    patchelf_bin = os.path.join(HOME_DIR, "patchelf")
    if not os.path.isfile(patchelf_bin):
        print("[=] Téléchargement de patchelf...")
        patchelf_url = "https://github.com/NixOS/patchelf/releases/download/0.18.0/patchelf-0.18.0-x86_64.tar.gz"
        patchelf_tar = os.path.join(HOME_DIR, "patchelf.tar.gz")
        urllib.request.urlretrieve(patchelf_url, patchelf_tar)
        with tarfile.open(patchelf_tar) as t:
            for m in t.getmembers():
                if m.name.endswith("patchelf"):
                    m.name = "patchelf"
                    t.extract(m, HOME_DIR)
                    break
        os.chmod(patchelf_bin, 0o755)
        os.remove(patchelf_tar)
        print("[✓] patchelf prêt.")

    # Télécharger les libs GLIBC 2.38
    libs_tar = os.path.join(HOME_DIR, "glibc-libs.tar.gz")
    if not os.path.isfile(libs_tar):
        print("[=] Téléchargement des libs GLIBC 2.38...")
        try:
            urllib.request.urlretrieve(GLIBC_LIBS_URL, libs_tar)
            with tarfile.open(libs_tar) as t:
                t.extractall(GLIBC_DIR)
            os.remove(libs_tar)
            print("[✓] Libs GLIBC 2.38 extraites.")
        except Exception as e:
            print(f"[!] Impossible de télécharger les libs GLIBC : {e}")
            print("[!] Upload glibc-2.38-libs.tar.gz dans le container ou recompile les binaires sur Debian 12.")
            sys.exit(1)

    # Patcher chaque binaire
    ld_path = None
    for f in os.listdir(GLIBC_DIR):
        if f.startswith("ld-linux") or f.startswith("ld-"):
            ld_path = os.path.join(GLIBC_DIR, f)
            break

    if ld_path is None:
        print(f"[!] ld-linux introuvable dans {GLIBC_DIR}")
        sys.exit(1)

    os.chmod(ld_path, 0o755)

    for key in BINARY_NAMES:
        b = find_binary(key)
        if b:
            print(f"[=] Patch {b}...")
            run(f"{patchelf_bin} --set-interpreter {ld_path} --set-rpath {GLIBC_DIR}:{BUILD_DIR}/thirdparty/mariadb-connector-cpp/src/mariadb_connector_cpp-build {b}")

    print("[✓] Binaires patchés pour GLIBC 2.38.")

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

# ─── Mode binaires pré-compilés ──────────────────────────────────────────────

def extract_prebuilt():
    print("\n[=] darkflame-bins.zip détecté → mode binaires pré-compilés")
    if find_binary("master") is not None:
        print("[✓] Binaires déjà extraits, skip.")
        return
    print("[=] Extraction des binaires...")
    os.makedirs(BUILD_DIR, exist_ok=True)
    with zipfile.ZipFile(BINS_ZIP, 'r') as z:
        z.extractall(BUILD_DIR)
    for key in BINARY_NAMES:
        for name in BINARY_NAMES[key]:
            p = os.path.join(BUILD_DIR, name)
            if os.path.isfile(p):
                os.chmod(p, 0o755)
    print("[✓] Binaires extraits dans", BUILD_DIR)

def install_runtime_deps():
    print("\n[=] Vérification des dépendances runtime...")
    apt = "sudo apt-get" if has_sudo() else "apt-get"
    r = subprocess.run(f"{apt} update -qq", shell=True)
    if r.returncode != 0:
        print("[!] apt-get indisponible, on continue.")
        return
    subprocess.run(f"{apt} install -y libmariadb3 libssl3 unzip", shell=True)

# ─── Mode compilation ────────────────────────────────────────────────────────

def install_build_deps():
    apt = "sudo apt-get" if has_sudo() else "apt-get"
    r = subprocess.run(f"{apt} update -qq", shell=True)
    if r.returncode != 0:
        print("[!] ERREUR : pas de droits sudo. Uploadez darkflame-bins.zip.")
        sys.exit(1)
    run(f"{apt} install -y git cmake g++ zlib1g-dev libssl-dev libmariadb-dev-compat libmariadb-dev unzip")

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

# ─── Commun ──────────────────────────────────────────────────────────────────

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

def write_config():
    print("\n[=] Écriture de la configuration...")
    raw = open(CONFIG_FILE).read()
    base_cfg = os.path.join(BUILD_DIR, "authconfig.ini")
    with open(base_cfg, "w") as f:
        f.write(raw)
    for c in ["masterconfig.ini", "worldconfig.ini", "chatconfig.ini"]:
        shutil.copy(base_cfg, os.path.join(BUILD_DIR, c))
    print("[✓] Configs écrites.")

def check_client_files(cfg):
    print("\n[=] Vérification des fichiers client...")
    client_path = cfg.get("General", "client_location", fallback="/home/container/client")
    if not os.path.isdir(client_path):
        print(f"[!] ERREUR : client introuvable à {client_path}")
        sys.exit(1)
    required = ["res/cdclient.fdb", "locale/locale.xml")
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
    setup_ld_library_path()
    print(f"[✓] Lancement de {master}")
    os.chdir(BUILD_DIR)
    os.execve(master, [master], os.environ)

# ─── Point d'entrée ──────────────────────────────────────────────────────────

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
    write_config()
    start_server()

if __name__ == "__main__":
    main()
