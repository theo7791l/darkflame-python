#!/usr/bin/env python3
"""
Darkflame Universe - Script d'installation et de démarrage
pour container Python Pterodactyl avec DB externe

Modes :
  - Mode BINAIRES : si /home/container/darkflame-bins.zip existe → skip compilation
  - Mode COMPILATION : sinon → clone + compile depuis les sources (nécessite sudo)
"""

import os
import subprocess
import sys
import shutil
import zipfile

# ─── Variables d'environnement ───────────────────────────────────────────────
MYSQL_HOST     = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT     = os.environ.get("MYSQL_PORT", "3306")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "darkflame")
MYSQL_USER     = os.environ.get("MYSQL_USER", "dlu_user")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
CLIENT_PATH    = os.environ.get("CLIENT_PATH", "/home/container/client")

HOME_DIR   = "/home/container"
BINS_ZIP   = os.path.join(HOME_DIR, "darkflame-bins.zip")
BUILD_DIR  = os.path.join(HOME_DIR, "darkflame-build")
SERVER_DIR = os.path.join(HOME_DIR, "DarkflameServer")

def run(cmd, cwd=None, check=True):
    print(f"[+] {cmd}")
    return subprocess.run(cmd, shell=True, cwd=cwd, check=check)

def has_sudo():
    result = subprocess.run("sudo -n true", shell=True, capture_output=True)
    return result.returncode == 0

# ─── Mode binaires pré-compilés ──────────────────────────────────────────────

def extract_prebuilt():
    print("\n[=] darkflame-bins.zip détecté → mode binaires pré-compilés")
    if os.path.exists(BUILD_DIR) and os.path.isfile(os.path.join(BUILD_DIR, "masterserver")):
        print("[✓] Binaires déjà extraits, skip.")
        return
    print("[=] Extraction des binaires...")
    os.makedirs(BUILD_DIR, exist_ok=True)
    with zipfile.ZipFile(BINS_ZIP, 'r') as z:
        z.extractall(BUILD_DIR)
    for binary in ["masterserver", "authserver", "chatserver", "worldserver"]:
        bin_path = os.path.join(BUILD_DIR, binary)
        if os.path.isfile(bin_path):
            os.chmod(bin_path, 0o755)
    print("[✓] Binaires extraits dans", BUILD_DIR)

def install_runtime_deps():
    """Libs minimales pour faire tourner les binaires (libmariadb, libssl)."""
    print("\n[=] Installation des dépendances runtime...")
    apt = "sudo apt-get" if has_sudo() else "apt-get"
    r = subprocess.run(f"{apt} update -qq", shell=True)
    if r.returncode != 0:
        print("[!] apt-get update échoué (pas de droits ?). On continue sans installer les libs.")
        print("    Si le serveur crash, les libs libmariadb3/libssl3 sont peut-être déjà présentes dans l'image.")
        return
    subprocess.run(f"{apt} install -y libmariadb3 libssl3 default-mysql-client unzip", shell=True)

# ─── Mode compilation depuis les sources ─────────────────────────────────────

def install_build_deps():
    print("\n[=] Installation des dépendances de compilation...")
    apt = "sudo apt-get" if has_sudo() else "apt-get"
    r = subprocess.run(f"{apt} update -qq", shell=True)
    if r.returncode != 0:
        print("[!] ERREUR : impossible d'installer les dépendances (pas de droits sudo).")
        print("    → Uploadez darkflame-bins.zip pour utiliser le mode binaires pré-compilés.")
        sys.exit(1)
    run(f"{apt} install -y git cmake g++ zlib1g-dev libssl-dev libmariadb-dev-compat libmariadb-dev default-mysql-client unzip")

def clone_server():
    if os.path.exists(SERVER_DIR):
        print("[=] DarkflameServer déjà cloné, mise à jour...")
        run("git pull", cwd=SERVER_DIR)
    else:
        print("[=] Clonage de DarkflameServer...")
        run(f"git clone --recursive https://github.com/DarkflameUniverse/DarkflameServer.git {SERVER_DIR}")

def build_server():
    master_bin = os.path.join(BUILD_DIR, "masterserver")
    if os.path.isfile(master_bin):
        print("[✓] Serveur déjà compilé, skip.")
        return
    print("\n[=] Compilation du serveur (peut prendre 5-15 min)...")
    os.makedirs(BUILD_DIR, exist_ok=True)
    run("cmake .. -DCMAKE_BUILD_TYPE=Release", cwd=BUILD_DIR)
    run("make -j$(nproc)", cwd=BUILD_DIR)
    print("\n[=] Nettoyage des fichiers de compilation...")
    for item in os.listdir(BUILD_DIR):
        item_path = os.path.join(BUILD_DIR, item)
        if item not in ["masterserver", "authserver", "chatserver", "worldserver"]:
            if os.path.isdir(item_path):
                shutil.rmtree(item_path, ignore_errors=True)
            elif item.endswith((".o", ".a", ".cmake")):
                os.remove(item_path)
    shutil.rmtree(SERVER_DIR, ignore_errors=True)
    print("[✓] Compilation + nettoyage terminés !")

# ─── Commun ──────────────────────────────────────────────────────────────────

def write_config():
    print("\n[=] Écriture de la configuration...")
    template_path = os.path.join(HOME_DIR, "config_template.ini")
    config_content = open(template_path).read() \
        .replace("{{MYSQL_HOST}}",     MYSQL_HOST) \
        .replace("{{MYSQL_PORT}}",     MYSQL_PORT) \
        .replace("{{MYSQL_DATABASE}}", MYSQL_DATABASE) \
        .replace("{{MYSQL_USER}}",     MYSQL_USER) \
        .replace("{{MYSQL_PASSWORD}}", MYSQL_PASSWORD) \
        .replace("{{CLIENT_PATH}}",    CLIENT_PATH)
    base_cfg = os.path.join(BUILD_DIR, "authconfig.ini")
    with open(base_cfg, "w") as f:
        f.write(config_content)
    for cfg in ["masterconfig.ini", "worldconfig.ini", "chatconfig.ini"]:
        shutil.copy(base_cfg, os.path.join(BUILD_DIR, cfg))
    print("[✓] Configs écrites.")

def check_client_files():
    print("\n[=] Vérification des fichiers client...")
    if not os.path.isdir(CLIENT_PATH):
        print(f"[!] ERREUR : fichiers client introuvables à {CLIENT_PATH}")
        sys.exit(1)
    required = ["res/cdclient.fdb", "locale/locale.xml"]
    missing = [f for f in required if not os.path.isfile(os.path.join(CLIENT_PATH, f))]
    if missing:
        print(f"[!] Fichiers client manquants : {', '.join(missing)}")
        sys.exit(1)
    print(f"[✓] Fichiers client OK à {CLIENT_PATH}")

def test_db_connection():
    print("\n[=] Test de connexion à la base de données...")
    try:
        result = subprocess.run(
            f"mysql -h {MYSQL_HOST} -P {MYSQL_PORT} -u {MYSQL_USER} -p{MYSQL_PASSWORD} -e 'SELECT 1;' {MYSQL_DATABASE}",
            shell=True, capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            print("[✓] Connexion DB réussie !")
        else:
            print(f"[!] ERREUR connexion DB : {result.stderr.strip()}")
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print("[!] Timeout connexion DB. Vérifiez MYSQL_HOST et le firewall.")
        sys.exit(1)

def start_server():
    print("\n[=] Démarrage de Darkflame Universe...")
    master = os.path.join(BUILD_DIR, "masterserver")
    if not os.path.isfile(master):
        print(f"[!] ERREUR : binaire masterserver introuvable à {master}")
        print("    → Uploadez darkflame-bins.zip dans /home/container/")
        sys.exit(1)
    os.chdir(BUILD_DIR)
    os.execv(master, [master])

# ─── Point d'entrée ──────────────────────────────────────────────────────────

def main():
    print("===================================================")
    print(" Darkflame Universe - Pterodactyl Python Container")
    print("===================================================")

    if os.path.isfile(BINS_ZIP):
        install_runtime_deps()
        extract_prebuilt()
    else:
        print("[!] Aucun darkflame-bins.zip trouvé → tentative de compilation")
        install_build_deps()
        clone_server()
        build_server()

    check_client_files()
    test_db_connection()
    write_config()
    start_server()

if __name__ == "__main__":
    main()
