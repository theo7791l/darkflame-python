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

HOME_DIR     = "/home/container"
BINS_ZIP     = os.path.join(HOME_DIR, "darkflame-bins.zip")
BUILD_DIR    = os.path.join(HOME_DIR, "darkflame-build")
SERVER_DIR   = os.path.join(HOME_DIR, "DarkflameServer")
CONFIG_FILE  = os.path.join(HOME_DIR, "config_template.ini")
GLIBC_DIR    = os.path.join(HOME_DIR, "glibc-compat")
PATCHELF     = os.path.join(HOME_DIR, "patchelf")
PATCHED_FLAG = os.path.join(HOME_DIR, ".glibc_patched")
EXTRACT_FLAG = os.path.join(HOME_DIR, ".bins_extracted")

# navmeshes.zip téléchargé depuis le repo DarkflameServer (resources/)
NAVMESHES_URL = "https://github.com/DarkflameUniverse/DarkflameServer/raw/main/resources/navmeshes.zip"

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
        "MYSQL_HOST":     ("Database", "mysql_host"),
        "MYSQL_PORT":     ("Database", "mysql_port"),
        "MYSQL_DATABASE": ("Database", "mysql_database"),
        "MYSQL_USER":     ("Database", "mysql_username"),
        "MYSQL_PASSWORD": ("Database", "mysql_password"),
        "CLIENT_PATH":    ("General",  "client_location"),
        "EXTERNAL_IP":    ("Networking", "external_ip"),
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
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, 'wb') as f:
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
            print(f"  [!] Symlink circulaire ignoré : {base} -> {link_target}")
            continue
        if not os.path.isfile(target_path):
            print(f"  [!] Cible absente pour symlink {base} -> {link_target}, skip")
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
                    print(f"[!] Symlink circulaire détecté et ignoré : {f} -> {target_name}")
                    continue
                real = os.path.join(glibc_dir, target_name)
                if os.path.isfile(real):
                    os.chmod(real, 0o755)
                    print(f"[✓] ld via symlink : {f} -> {target_name}")
                    return p
                else:
                    print(f"[!] Cible du symlink absente : {target_name}")
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
                print(f"[✓] ld fallback : {f} -> {resolved}")
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
    key_libs = [f for f in libs if 'libc' in f or 'ld-linux' in f or 'ld-2.' in f or 'libstdc' in f or 'libgcc' in f]
    print(f"[=] Libs disponibles : {key_libs}")

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
        print(f"[!] ld-linux introuvable. Fichiers disponibles : {[f for f in libs if 'ld' in f]}")
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
            else:
                print(f"[!] patchelf a échoué sur {os.path.basename(b)}")

    if patched_count > 0:
        open(PATCHED_FLAG, 'w').close()
        print(f"[✓] {patched_count} binaire(s) patché(s) avec GLIBC 2.39.")
    else:
        print("[!] Aucun binaire patché, flag non créé.")


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
        print("[=] Nettoyage extraction précédente incomplète...")
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


def fetch_navmeshes():
    """
    Télécharge resources/navmeshes.zip depuis DarkflameServer
    et extrait les .bin dans BUILD_DIR/res/maps/navmeshes/
    
    La vérification se fait sur le dossier final attendu par MasterServer :
      darkflame-build/navmeshes/   (chemin vérifié par MasterServer.cpp:112)
    """
    # MasterServer cherche navmeshes/ à côté du binaire (dans BUILD_DIR)
    dest = os.path.join(BUILD_DIR, "navmeshes")
    if os.path.isdir(dest) and any(f.endswith(".bin") for f in os.listdir(dest)):
        print("[✓] navmeshes/ déjà présent, skip.")
        return

    print("[=] Téléchargement navmeshes.zip depuis DarkflameServer/resources...")
    zip_path = os.path.join(HOME_DIR, "navmeshes.zip")
    try:
        download_file(NAVMESHES_URL, zip_path)
    except Exception as e:
        print(f"[!] Échec téléchargement navmeshes.zip : {e}")
        sys.exit(1)

    os.makedirs(dest, exist_ok=True)
    print(f"[=] Extraction navmeshes.zip → {dest} ...")
    with zipfile.ZipFile(zip_path, 'r') as z:
        for member in z.namelist():
            filename = os.path.basename(member)
            if not filename or not filename.endswith(".bin"):
                continue
            with z.open(member) as src, open(os.path.join(dest, filename), 'wb') as out:
                shutil.copyfileobj(src, out)

    bin_count = len([f for f in os.listdir(dest) if f.endswith(".bin")])
    os.remove(zip_path)

    if bin_count == 0:
        print("[!] ERREUR : navmeshes.zip ne contenait aucun .bin !")
        sys.exit(1)

    print(f"[✓] {bin_count} navmesh(es) extrait(s) dans {dest}")


def setup_server_data(cfg):
    """
    Copie res/ et locale/ depuis le client vers BUILD_DIR.
    Télécharge et extrait navmeshes/ depuis DarkflameServer/resources/navmeshes.zip.
    """
    print("\n[=] Vérification des données serveur (navmeshes, res, locale)...")
    client_path = cfg.get("General", "client_location", fallback="/home/container/client")

    # res/ et locale/ viennent du client LEGO Universe
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

    # navmeshes vient de DarkflameServer/resources/navmeshes.zip
    fetch_navmeshes()

    # Créer le dossier logs
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


def write_config(cfg):
    print("\n[=] Écriture de la configuration...")
    os.makedirs(BUILD_DIR, exist_ok=True)
    raw_lines = []
    for section in cfg.sections():
        raw_lines.append(f"[{section}]")
        for key, val in cfg.items(section):
            raw_lines.append(f"{key}={val}")
        raw_lines.append("")
    raw = "\n".join(raw_lines)
    for config_name in ["authconfig.ini", "masterconfig.ini", "worldconfig.ini", "chatconfig.ini"]:
        dest = os.path.join(BUILD_DIR, config_name)
        with open(dest, "w") as f:
            f.write(raw)
    print("[✓] Configs écrites (authconfig, masterconfig, worldconfig, chatconfig).")


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
