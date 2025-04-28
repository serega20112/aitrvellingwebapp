-- schema.sql - Убедись, что у тебя есть все эти таблицы

DROP TABLE IF EXISTS visited_places;
DROP TABLE IF EXISTS user_interests;
DROP TABLE IF EXISTS interests;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS cache;

CREATE TABLE cache (...); -- Полное определение
CREATE INDEX idx_cache_timestamp ON cache (timestamp);

CREATE TABLE users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX idx_users_username ON users (username);

CREATE TABLE interests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL COLLATE NOCASE
);
CREATE UNIQUE INDEX idx_interests_name ON interests (name);

CREATE TABLE user_interests (
  user_id INTEGER NOT NULL,
  interest_id INTEGER NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
  FOREIGN KEY (interest_id) REFERENCES interests (id) ON DELETE CASCADE,
  PRIMARY KEY (user_id, interest_id)
);
CREATE INDEX idx_user_interests_user ON user_interests (user_id);
CREATE INDEX idx_user_interests_interest ON user_interests (interest_id);

CREATE TABLE visited_places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    place_name TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    visited_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX idx_visited_places_user ON visited_places (user_id);

-- Предзаполнение интересов (опционально)
INSERT INTO interests (name) VALUES ('History'), ('Architecture'), ('Nature'), ('Food'), ('Art'), ('Music'), ('Shopping'), ('Nightlife'), ('Adventure'), ('Relaxation'), ('Technology'), ('Local Culture'), ('Museums'), ('Parks');