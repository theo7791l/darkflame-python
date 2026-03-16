# Darkflame Python 🧱

Héberge un serveur **Darkflame Universe** (LEGO Universe) sur un container **Python Pterodactyl** avec une base de données MariaDB/MySQL **externe**.

## Prérequis

- Un panel Pterodactyl avec un egg Python (3.x)
- Une base de données MariaDB/MySQL distante déjà créée
- Les fichiers client LEGO Universe (client légal requis)
- Accès à l'admin Pterodactyl pour configurer les variables d'environnement

## Variables d'environnement à configurer dans Pterodactyl

| Variable | Description | Exemple |
|---|---|---|
| `MYSQL_HOST` | IP ou hostname de ta DB externe | `db.example.com` |
| `MYSQL_PORT` | Port MySQL | `3306` |
| `MYSQL_DATABASE` | Nom de la base de données | `darkflame` |
| `MYSQL_USER` | Utilisateur MySQL | `dlu_user` |
| `MYSQL_PASSWORD` | Mot de passe MySQL | `motdepasse` |
| `CLIENT_PATH` | Chemin vers les fichiers client | `/home/container/client` |

## Installation

1. Clone ce repo dans le container ou configure le startup script Pterodactyl
2. Place tes fichiers client LEGO Universe dans `/home/container/client`
3. Configure les variables d'environnement dans Pterodactyl
4. Lance `python3 install.py` au démarrage (géré automatiquement par Pterodactyl)

## Structure

```
darkflame-python/
├── install.py          # Script principal : installe et démarre Darkflame
├── setup_db.sql        # SQL pour créer la structure de la DB
├── config_template.ini # Template de configuration DarkflameServer
├── egg-darkflame.json  # Egg Pterodactyl prêt à importer
└── README.md
```

## Notes importantes

- La base de données doit être créée manuellement (voir `setup_db.sql`)
- Le port 1000 (auth), 2000 (chat), 3000 (world) doivent être ouverts
- Ne pas oublier d'autoriser l'IP du serveur Pterodactyl sur le firewall de la DB

## Crédits

- [DarkflameUniverse/DarkflameServer](https://github.com/DarkflameUniverse/DarkflameServer)
