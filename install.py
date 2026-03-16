#!/usr/bin/env python3
"""
Darkflame Universe - Script d'installation et de démarrage
pour container Python Pterodactyl avec DB externe
"""

import os
import subprocess
import sys
import time
import shutil

# ─── Variables d'environnement ───────────────────────────────────────────────
MYSQL_HOST     = os.environ.get("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT     = os.environ.get("MYSQL_PORT", "3306")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE", "darkflame")
MYSQL_USER     = os.environ.get("MYSQL_USER", "dlu_user")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD", "")
CLIENT_PATH    = os.environ.get("CLIENT_PATH", "/home/container/client")
SERVER_DIR     = "/home/container/DarkflameServer"
BUILD_DIR      = os.path.join(SERVER_DIR, "build")

def run(cmd, cwd=None, check=True):
    """Exécute une commande shell."""
    print(f"[+] {cmd}")
    result = subprocess.run(cmd, shell=True, cwd=cwd, check=check)
    return result

def install_dependencies():
    print("\n[=] Installation des dépendances système...")
    run("apt-get update -qq")
    run("apt-get install -y git cmake g++ zlib1g-dev libssl-dev libmariadb-dev-compat libmariadb-dev default-mysql-client")

def clone_server():
    if os.path.exists(SERVER_DIR):
        print("[=] DarkflameServer déjà cloné, mise à jour...")
        run("git pull", cwd=SERVER_DIR)
    else:
        print("[=] Clonage de DarkflameServer...")
        run(f"git clone --recursive https://github.com/DarkflameUniverse/DarkflameServer.git {SERVER_DIR}")

def build_server():
    print("\n[=] Compilation du serveur...")
    os.makedirs(BUILD_DIR, exist_ok=True)
    run("cmake .. -DCMAKE_BUILD_TYPE=Release", cwd=BUILD_DIR)
    run(f"make -j$(nproc)", cwd=BUILD_DIR)
    print("[✓] Compilation terminée !")

def write_config():
    print("\n[=] Écriture de la configuration...")
    config_template = open("/home/container/config_template.ini").read()
    config_content = config_template \
        .replace("{{MYSQL_HOST}}", MYSQL_HOST) \
        .replace("{{MYSQL_PORT}}", MYSQL_PORT) \
        .replace("{{MYSQL_DATABASE}}", MYSQL_DATABASE) \
        .replace("{{MYSQL_USER}}", MYSQL_USER) \
        .replace("{{MYSQL_PASSWORD}}", MYSQL_PASSWORD) \
        .replace("{{CLIENT_PATH}}", CLIENT_PATH)

    config_dest = os.path.join(BUILD_DIR, "authconfig.ini")
    with open(config_dest, "w") as f:
        f.write(config_content)

    # Copier pour master/world aussi
    for cfg in ["masterconfig.ini", "worldconfig.ini", "chatconfig.ini"]:
        shutil.copy(config_dest, os.path.join(BUILD_DIR, cfg))

    print("[✓] Configs écrites.")

def check_client_files():
    print("\n[=] Vérification des fichiers client...")
    if not os.path.isdir(CLIENT_PATH):
        print(f"[!] ERREUR : Les fichiers client sont introuvables à {CLIENT_PATH}")
        print("    Uploadez vos fichiers client LEGO Universe dans ce dossier.")
        sys.exit(1)
    else:
        print(f"[✓] Fichiers client trouvés à {CLIENT_PATH}")

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
            print(f"[!] ERREUR connexion DB : {result.stderr}")
            sys.exit(1)
    except subprocess.TimeoutExpired:
        print("[!] Timeout connexion DB. Vérifiez MYSQL_HOST et le firewall.")
        sys.exit(1)

def start_server():
    print("\n[=] Démarrage de Darkflame Universe...")
    os.chdir(BUILD_DIR)
    # Démarrage du master server (lance auth, chat, world automatiquement)
    os.execv(f"{BUILD_DIR}/masterserver", [f"{BUILD_DIR}/masterserver"])

def main():
    print("===================================================")
    print(" Darkflame Universe - Pterodactyl Python Container")
    print("===================================================")

    install_dependencies()
    clone_server()
    build_server()
    check_client_files()
    test_db_connection()
    write_config()
    start_server()

if __name__ == "__main__":
    main()
