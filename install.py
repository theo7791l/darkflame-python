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

HOME_DIR    = "/home/container"
BINS_ZIP    = os.path.join(HOME_DIR, "darkflame-bins.zip")
BUILD_DIR   = os.path.join(HOME_DIR, "darkflame-build")
SERVER_DIR  = os.path.join(HOME_DIR, "DarkflameServer")
CONFIG_FILE = os.path.join(HOME_DIR, "config_template.ini")
GLIBC_DIR   = os.path.join(HOME_DIR, "glibc-compat")
PATCHELF    = os.path.join(HOME_DIR, "patchelf")

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
    master = find_binary("master")
    if master is None:
        return False
    r = subprocess.run(["ldd", master], capture_output=True, text=True)
    output = r.stdout + r.stderr
    return "GLIBC_2.38" in output or "GLIBC_2.39" in output or "not found" in output

def download_file(url, dest):
    print(f"[=] Téléchargement {url.split('/')[-1]}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Wget/1.21"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, 'wb') as f:
        shutil.copyfileobj(r, f)

def extract_so_from_tar(t, out_dir):
    """
    Extrait depuis un tarfile ouvert :
    - les fichiers .so (réguliers)
    - les symlinks .so (recréés comme vrais symlinks)
    """
    members = t.getmembers()
    # D'abord les fichiers réguliers
    for m in members:
        base = os.path.basename(m.name)
        if ".so" not in base:
            continue
        dest_path = os.path.join(out_dir, base)
        if m.isfile():
            try:
                src = t.extractfile(m)
                if src:
                    with open(dest_path, 'wb') as f:
                        shutil.copyfileobj(src, f)
                    os.chmod(dest_path, 0o755)
            except Exception as e:
                print(f"  [!] Erreur extraction {base}: {e}")
        elif m.issym():
            # Symlink : recréer le lien
            link_target = m.linkname
            try:
                if os.path.lexists(dest_path):
                    os.remove(dest_path)
                os.symlink(link_target, dest_path)
            except Exception as e:
                print(f"  [!] Erreur symlink {base} -> {link_target}: {e}")

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

def setup_glibc_compat():
    if os.path.isdir(GLIBC_DIR) and any('libc' in f for f in os.listdir(GLIBC_DIR)):
        print("[✓] GLIBC compat déjà présent, skip.")
        return

    print("[=] GLIBC_2.38 requis → téléchargement libs Ubuntu 24.04...")
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
    print(f"[=] Libs extraites : {[f for f in libs if 'libc' in f or 'ld-' in f or 'libstdc' in f or 'libgcc' in f]}")
    if not any('libc' in f for f in libs):
        print("[!] ERREUR : libc.so.6 non trouvé après extraction")
        sys.exit(1)

    # patchelf
    if not os.path.isfile(PATCHELF):
        print("[=] Téléchargement patchelf...")
        tar = os.path.join(HOME_DIR, "patchelf.tar.gz")
        download_file(PATCHELF_URL, tar)
        with tarfile.open(tar) as t:
            for m in t.getmembers():
                if m.name.endswith("patchelf") and m.isfile():
                    src = t.extractfile(m)
                    with open(PATCHELF, 'wb') as out:
                        shutil.copyfileobj(src, out)
                    break
        os.chmod(PATCHELF, 0o755)
        os.remove(tar)

    # Trouver ld-linux (fichier réel ou symlink résolu)
    ld = None
    for f in os.listdir(GLIBC_DIR):
        if f.startswith("ld-linux") or f.startswith("ld-"):
            candidate = os.path.join(GLIBC_DIR, f)
            # Résoudre si symlink
            if os.path.islink(candidate):
                target = os.readlink(candidate)
                # Si target relatif, chercher dans GLIBC_DIR
                if not os.path.isabs(target):
                    target = os.path.join(GLIBC_DIR, os.path.basename(target))
                if os.path.isfile(target):
                    ld = candidate
                    break
            elif os.path.isfile(candidate):
                ld = candidate
                break

    if ld is None:
        print(f"[!] ld-linux introuvable. Fichiers dispo : {[f for f in libs if 'ld' in f]}")
        sys.exit(1)

    # S'assurer que le fichier réel est exécutable
    real_ld = os.path.realpath(ld)
    if os.path.isfile(real_ld):
        os.chmod(real_ld, 0o755)
    os.chmod(ld, 0o755) if not os.path.islink(ld) else None

    mariadb_lib = os.path.join(BUILD_DIR, "thirdparty", "mariadb-connector-cpp",
                               "src", "mariadb_connector_cpp-build")
    rpath = f"{GLIBC_DIR}:{BUILD_DIR}:{mariadb_lib}"

    for key in BINARY_NAMES:
        b = find_binary(key)
        if b:
            print(f"[=] Patch {os.path.basename(b)} → GLIBC compat...")
            run(f"{PATCHELF} --set-interpreter {ld} --set-rpath {rpath} {b}")

    print("[✓] Binaires patchés avec GLIBC 2.39.")

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
    subprocess.run(f"{apt} install -y libmariadb3 libssl3 unzip binutils", shell=True)

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
    write_config()
    start_server()

if __name__ == "__main__":
    main()
