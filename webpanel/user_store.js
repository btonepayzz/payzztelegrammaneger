"use strict";

const fs = require("fs");
const path = require("path");
const bcrypt = require("bcryptjs");

function usersPath() {
  return (process.env.WEB_PANEL_USERS_FILE || "").trim() || path.join(__dirname, "panel_users.json");
}

function normalizeUsername(u) {
  return String(u || "")
    .trim()
    .toLowerCase();
}

function loadData() {
  const p = usersPath();
  if (!fs.existsSync(p)) return { users: [] };
  const raw = fs.readFileSync(p, "utf8");
  const data = JSON.parse(raw);
  if (!data || !Array.isArray(data.users)) return { users: [] };
  return data;
}

function saveData(data) {
  const p = usersPath();
  const dir = path.dirname(p);
  if (dir && dir !== "." && !fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }
  const tmp = p + ".tmp";
  const body = JSON.stringify({ v: 1, users: data.users }, null, 2);
  fs.writeFileSync(tmp, body, { mode: 0o600 });
  fs.renameSync(tmp, p);
}

/** Dosya yoksa .env kullanıcısından tek yönetici kaydı oluşturur. */
function initFromEnvIfEmpty() {
  const p = usersPath();
  if (fs.existsSync(p)) {
    const d = loadData();
    if (d.users.length) return;
  }
  const wantUser = normalizeUsername(process.env.WEB_PANEL_USER || "");
  if (!wantUser) {
    throw new Error("WEB_PANEL_USER tanımlı değil veya panel_users.json boş");
  }
  const hashEnv = (process.env.WEB_PANEL_PASSWORD_HASH || "").trim();
  const plain = process.env.WEB_PANEL_PASSWORD || "";
  let password_hash;
  if (hashEnv) password_hash = hashEnv;
  else if (plain) password_hash = bcrypt.hashSync(plain, 10);
  else throw new Error("WEB_PANEL_PASSWORD veya WEB_PANEL_PASSWORD_HASH gerekli (ilk kurulum)");
  saveData({
    users: [
      {
        username: wantUser,
        password_hash,
        active: true,
        admin: true,
        created_at: new Date().toISOString(),
      },
    ],
  });
}

function findUser(username) {
  const un = normalizeUsername(username);
  return loadData().users.find((x) => x.username === un) || null;
}

function verifyLogin(username, password) {
  const data = loadData();
  if (!data.users.length) {
    return {
      ok: false,
      error:
        "Panel kullanıcı listesi boş. .env içinde WEB_PANEL_USER ve WEB_PANEL_PASSWORD tanımlayıp paneli yeniden başlatın (panel_users.json otomatik oluşur).",
      status: 503,
    };
  }
  const un = normalizeUsername(username);
  const u = data.users.find((x) => x.username === un);
  if (!u) return { ok: false, error: "Kullanıcı adı veya şifre hatalı", status: 401 };
  if (!u.active) return { ok: false, error: "Bu hesap devre dışı bırakıldı.", status: 403 };
  const ok = bcrypt.compareSync(password || "", u.password_hash);
  if (!ok) return { ok: false, error: "Kullanıcı adı veya şifre hatalı", status: 401 };
  return { ok: true, username: u.username, is_admin: !!u.admin };
}

function validNewUsername(raw) {
  const u = normalizeUsername(raw);
  if (!/^[a-z0-9_]{3,32}$/.test(u)) {
    return { ok: false, error: "Kullanıcı adı 3–32 karakter; yalnızca a–z, 0–9, _" };
  }
  return { ok: true, username: u };
}

function countActiveAdmins(data) {
  return data.users.filter((x) => x.active && x.admin).length;
}

function listUsersSanitized() {
  return loadData().users.map((u) => ({
    username: u.username,
    active: !!u.active,
    admin: !!u.admin,
    created_at: u.created_at || null,
    totp_enrolled: !!(u.totp_secret && String(u.totp_secret).trim()),
  }));
}

/** Kullanıcı kayıtlı TOTP secret'ı (yoksa null). */
function getUserTotpSecret(username) {
  const u = findUser(username);
  if (!u || !u.totp_secret) return null;
  const s = String(u.totp_secret).trim();
  return s || null;
}

function setUserTotpSecret(username, secret) {
  const un = normalizeUsername(username);
  const data = loadData();
  const u = data.users.find((x) => x.username === un);
  if (!u) throw new Error("Kullanıcı bulunamadı");
  u.totp_secret = String(secret).trim();
  saveData(data);
}

/**
 * Eski tek-secret (panel_totp.secret / env) kurulumunu tek kullanıcıya taşır.
 * Birden fazla panel kullanıcısı varsa false döner (herkes kendi QR ile kurar).
 */
function migrateGlobalTotpIfSingleUserWithoutMfa(globalSecret) {
  const g = String(globalSecret || "").trim();
  if (!g) return false;
  const data = loadData();
  if (data.users.length !== 1) return false;
  const u = data.users[0];
  if (u.totp_secret && String(u.totp_secret).trim()) return false;
  u.totp_secret = g;
  saveData(data);
  return true;
}

function createUser({ username, password, admin, active }, actorUsername) {
  const actor = findUser(actorUsername);
  if (!actor || !actor.admin || !actor.active) {
    return { ok: false, error: "Yetkisiz", status: 403 };
  }
  const vu = validNewUsername(username);
  if (!vu.ok) return { ok: false, error: vu.error, status: 400 };
  if (!password || String(password).length < 8) {
    return { ok: false, error: "Şifre en az 8 karakter olmalı", status: 400 };
  }
  const data = loadData();
  if (data.users.some((x) => x.username === vu.username)) {
    return { ok: false, error: "Bu kullanıcı adı zaten var", status: 409 };
  }
  const isAdmin = !!admin;
  const isActive = active !== false;
  data.users.push({
    username: vu.username,
    password_hash: bcrypt.hashSync(String(password), 10),
    active: isActive,
    admin: isAdmin,
    created_at: new Date().toISOString(),
  });
  saveData(data);
  return { ok: true };
}

function updateUser(targetUsername, body, actorUsername) {
  const actor = findUser(actorUsername);
  if (!actor || !actor.admin || !actor.active) {
    return { ok: false, error: "Yetkisiz", status: 403 };
  }
  const un = normalizeUsername(targetUsername);
  const data = loadData();
  const idx = data.users.findIndex((x) => x.username === un);
  if (idx === -1) return { ok: false, error: "Kullanıcı bulunamadı", status: 404 };
  const u = data.users[idx];
  const self = normalizeUsername(actorUsername) === un;

  if (body.password != null && String(body.password).length > 0) {
    if (String(body.password).length < 8) {
      return { ok: false, error: "Şifre en az 8 karakter", status: 400 };
    }
    u.password_hash = bcrypt.hashSync(String(body.password), 10);
  }

  if (typeof body.active === "boolean" && body.active !== u.active) {
    if (self && !body.active) {
      return { ok: false, error: "Kendi hesabınızı devre dışı bırakamazsınız", status: 400 };
    }
    if (!body.active && u.admin && countActiveAdmins(data) <= 1) {
      return { ok: false, error: "Son aktif yöneticiyi kapatamazsınız", status: 400 };
    }
    u.active = body.active;
  }

  if (typeof body.admin === "boolean" && body.admin !== u.admin) {
    if (u.admin && !body.admin && countActiveAdmins(data) <= 1) {
      return { ok: false, error: "Sistemde en az bir aktif yönetici kalmalı", status: 400 };
    }
    u.admin = body.admin;
  }

  if (body.totp_reset === true) {
    delete u.totp_secret;
  }

  saveData(data);
  return { ok: true };
}

module.exports = {
  usersPath,
  initFromEnvIfEmpty,
  verifyLogin,
  findUser,
  listUsersSanitized,
  createUser,
  updateUser,
  normalizeUsername,
  getUserTotpSecret,
  setUserTotpSecret,
  migrateGlobalTotpIfSingleUserWithoutMfa,
};
