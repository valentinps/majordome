#!/bin/bash
# Runs once, on first Postgres start. Creates one role + database per component.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username postgres <<SQL
CREATE ROLE synapse          LOGIN PASSWORD '${SYNAPSE_DB_PASSWORD}';
CREATE ROLE mautrix_whatsapp LOGIN PASSWORD '${WHATSAPP_DB_PASSWORD}';
CREATE ROLE mautrix_discord  LOGIN PASSWORD '${DISCORD_DB_PASSWORD}';
CREATE ROLE majordome        LOGIN PASSWORD '${MAJORDOME_DB_PASSWORD}';

-- Synapse requires C collation.
CREATE DATABASE synapse OWNER synapse ENCODING 'UTF8'
  LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
CREATE DATABASE mautrix_whatsapp OWNER mautrix_whatsapp;
CREATE DATABASE mautrix_discord  OWNER mautrix_discord;
CREATE DATABASE majordome        OWNER majordome;
SQL

psql -v ON_ERROR_STOP=1 --username postgres --dbname majordome <<SQL
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;
SQL
