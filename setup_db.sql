-- ============================================================
-- Darkflame Universe - Création de la base de données
-- À exécuter sur ta DB externe AVANT de lancer le serveur
-- mysql -h HOST -u root -p < setup_db.sql
-- ============================================================

CREATE DATABASE IF NOT EXISTS `darkflame` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'dlu_user'@'%' IDENTIFIED BY 'CHANGE_MOI';
GRANT ALL PRIVILEGES ON `darkflame`.* TO 'dlu_user'@'%';
FLUSH PRIVILEGES;

-- Les tables seront créées automatiquement par DarkflameServer au premier lancement
-- via les migrations SQL embarquées dans le projet.
SELECT 'Base de données darkflame créée avec succès !' AS status;
