"use strict";

const fs = require("fs");
const path = require("path");
const express = require("express");
const session = require("express-session");
const bcrypt = require("bcryptjs");
const QRCode = require("qrcode");
const { authenticator } = require("otplib");
const userStore = require("./user_store");

const INTERNAL = (process.env.INTERNAL_API_URL || "http://127.0.0.1:18765").replace(/\/$/, "");
const TOKEN = process.env.INTERNAL_PANEL_TOKEN || "";
const PORT = parseInt(process.env.WEB_PANEL_PORT || process.env.PORT || "3080", 10);
const BIND = (process.env.WEB_PANEL_BIND_HOST || "127.0.0.1").trim();
const TOTP_CHALLENGE_MS = 10 * 60 * 1000;

function totpSecretPath() {
  return (process.env.WEB_PANEL_TOTP_FILE || "").trim() || path.join(__dirname, "panel_totp.secret");
}

function readTotpSecretFromFile() {
  const p = totpSecretPath();
  try {
    const s = fs.readFileSync(p, "utf8").trim();
    return s || null;
  } catch {
    return null;
  }
}

/** Ortam değişkeni doluysa o; değilse dosyadan okunan secret. */
function getEffectiveTotpSecret() {
  const env = (process.env.WEB_PANEL_TOTP_SECRET || "").trim();
  if (env) return env;
  return readTotpSecretFromFile();
}

async function buildTotpProvisioning(username, secret) {
  const otpauth_url = authenticator.keyuri(username, "PAYZZ TELEGRAM YÖNETİM", secret);
  const qr_data_url = await QRCode.toDataURL(otpauth_url, {
    width: 220,
    margin: 2,
    errorCorrectionLevel: "M",
  });
  return { secret, otpauth_url, qr_data_url };
}

try {
  userStore.initFromEnvIfEmpty();
} catch (e) {
  console.warn("[panel] Kullanıcı deposu:", e.message);
}
try {
  const migrated = userStore.migrateGlobalTotpIfSingleUserWithoutMfa(getEffectiveTotpSecret());
  if (migrated && !(process.env.WEB_PANEL_TOTP_SECRET || "").trim()) {
    const p = totpSecretPath();
    if (fs.existsSync(p)) {
      fs.unlinkSync(p);
      console.info("[panel] Eski panel_totp.secret tek kullanıcıya taşındı; dosya kaldırıldı.");
    }
  }
} catch (e) {
  console.warn("[panel] TOTP taşıma:", e.message);
}

async function pyFetch(apiPath, opts = {}) {
  const url = INTERNAL + apiPath;
  const headers = {
    Authorization: "Bearer " + TOKEN,
    ...opts.headers,
  };
  if (opts.body && typeof opts.body === "object" && !(opts.body instanceof Buffer)) {
    headers["Content-Type"] = "application/json";
    opts = { ...opts, body: JSON.stringify(opts.body) };
  }
  return fetch(url, { ...opts, headers });
}

function requireAuth(req, res, next) {
  if (req.session && req.session.authenticated) return next();
  if (req.accepts("html")) return res.redirect("/");
  res.status(401).json({ error: "Giriş gerekli" });
}

function requireAdmin(req, res, next) {
  if (!req.session || !req.session.authenticated) {
    if (req.accepts("html")) return res.redirect("/");
    return res.status(401).json({ error: "Giriş gerekli" });
  }
  if (!req.session.isAdmin) {
    if (req.accepts("html")) return res.redirect("/dashboard");
    return res.status(403).json({ error: "Yönetim için yetkiniz yok" });
  }
  next();
}

const app = express();
if (process.env.RAILWAY_ENVIRONMENT) {
  app.set("trust proxy", 1);
}
app.use(express.json());
app.use(
  session({
    name: "anabot.sid",
    secret: process.env.WEB_PANEL_SESSION_SECRET || "change-me",
    resave: false,
    saveUninitialized: false,
    /** Oturum silindiğinde çerezin sunucu yanıtında da temizlenmesi için gerekli. */
    unset: "destroy",
    cookie: {
      httpOnly: true,
      sameSite: "strict",
      secure: Boolean(process.env.RAILWAY_ENVIRONMENT),
      maxAge: 86400000,
      path: "/",
    },
  }),
);

/** Oturum / panel sayfalarının önbelleğe alınmasını engeller (geri tuşu ile “sahte” giriş). */
app.use((req, res, next) => {
  res.setHeader("Cache-Control", "private, no-store, no-cache, must-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.setHeader("Expires", "0");
  next();
});

/** 1. adım: yalnızca kullanıcı adı + şifre → 2FA pop-up için bilgi döner. */
app.post("/api/login", async (req, res) => {
  const { username, password } = req.body || {};
  const v = userStore.verifyLogin(username, password);
  if (!v.ok) return res.status(v.status).json({ error: v.error });

  delete req.session.authenticated;
  delete req.session.totpSetupSecret;
  req.session.pendingTotp = true;
  req.session.pendingLoginUsername = v.username;
  req.session.pendingLoginIsAdmin = !!v.is_admin;
  req.session.loginChallengeAt = Date.now();

  const userTotp = userStore.getUserTotpSecret(v.username);
  if (userTotp) {
    return res.json({ ok: true, step: "totp", enrolled: true });
  }

  const secret = authenticator.generateSecret();
  req.session.totpSetupSecret = secret;
  try {
    const provisioning = await buildTotpProvisioning(String(v.username || "panel"), secret);
    return res.json({ ok: true, step: "totp", enrolled: false, provisioning });
  } catch (e) {
    delete req.session.pendingTotp;
    delete req.session.loginChallengeAt;
    delete req.session.totpSetupSecret;
    delete req.session.pendingLoginUsername;
    delete req.session.pendingLoginIsAdmin;
    return res.status(500).json({ error: "2FA kurulum verisi oluşturulamadı: " + String(e.message || e) });
  }
});

/** 2. adım: TOTP kodu (kurulum veya normal giriş). */
app.post("/api/login/totp", (req, res) => {
  if (!req.session.pendingTotp) {
    return res.status(401).json({ error: "Önce kullanıcı adı ve şifre ile giriş yapın." });
  }
  const at = req.session.loginChallengeAt || 0;
  if (Date.now() - at > TOTP_CHALLENGE_MS) {
    delete req.session.pendingTotp;
    delete req.session.loginChallengeAt;
    delete req.session.totpSetupSecret;
    delete req.session.pendingLoginUsername;
    delete req.session.pendingLoginIsAdmin;
    return res.status(401).json({ error: "Süre doldu; tekrar kullanıcı adı ve şifre girin." });
  }

  const token = String((req.body || {}).totp || "").replace(/\s/g, "");
  if (!/^\d{6,8}$/.test(token)) {
    return res.status(400).json({ error: "Geçerli bir doğrulayıcı kodu girin (6 hane)." });
  }

  const setupSecret = req.session.totpSetupSecret;
  const pendingUser = req.session.pendingLoginUsername;
  if (setupSecret) {
    const ok = authenticator.verify({ token, secret: setupSecret });
    if (!ok) return res.status(401).json({ error: "Kod geçersiz veya süresi doldu. Authenticator saatini kontrol edin." });
    try {
      userStore.setUserTotpSecret(pendingUser, setupSecret);
    } catch (e) {
      return res.status(500).json({ error: "2FA kaydedilemedi: " + String(e.message || e) });
    }
  } else {
    const secret = userStore.getUserTotpSecret(pendingUser);
    if (!secret) {
      delete req.session.pendingTotp;
      delete req.session.loginChallengeAt;
      return res.status(500).json({ error: "Bu hesap için TOTP tanımlı değil; önce şifre adımından tekrar deneyin." });
    }
    const ok = authenticator.verify({ token, secret });
    if (!ok) return res.status(401).json({ error: "2FA kodu geçersiz." });
  }

  const panelUsername = req.session.pendingLoginUsername;
  const isAdmin = !!req.session.pendingLoginIsAdmin;
  req.session.regenerate((err) => {
    if (err) return res.status(500).json({ error: "Oturum oluşturulamadı" });
    req.session.authenticated = true;
    req.session.panelUsername = panelUsername;
    req.session.isAdmin = isAdmin;
    res.json({ ok: true });
  });
});

/** İlk adımdan vazgeç (pop-up kapatıldığında). */
app.post("/api/login/cancel", (req, res) => {
  delete req.session.pendingTotp;
  delete req.session.loginChallengeAt;
  delete req.session.totpSetupSecret;
  delete req.session.pendingLoginUsername;
  delete req.session.pendingLoginIsAdmin;
  res.json({ ok: true });
});

app.get("/api/me", requireAuth, (req, res) => {
  res.json({
    username: req.session.panelUsername || null,
    is_admin: !!req.session.isAdmin,
  });
});

app.get("/api/admin/users", requireAuth, requireAdmin, (req, res) => {
  res.json({ ok: true, users: userStore.listUsersSanitized() });
});

app.post("/api/admin/users", requireAuth, requireAdmin, (req, res) => {
  const body = req.body || {};
  const r = userStore.createUser(
    {
      username: body.username,
      password: body.password,
      admin: !!body.admin,
      active: body.active !== false,
    },
    req.session.panelUsername
  );
  if (!r.ok) return res.status(r.status).json({ error: r.error });
  res.json({ ok: true });
});

app.patch("/api/admin/users/:name", requireAuth, requireAdmin, (req, res) => {
  const r = userStore.updateUser(req.params.name, req.body || {}, req.session.panelUsername);
  if (!r.ok) return res.status(r.status).json({ error: r.error });
  res.json({ ok: true });
});

app.get("/api/admin/telethon/status", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/telethon/status");
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/admin/telethon/send_code", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/telethon/send_code", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/admin/telethon/sign_in", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/telethon/sign_in", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/admin/telethon/password", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/telethon/password", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.get("/api/admin/tokens", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/admin/tokens");
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/admin/tokens", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/admin/tokens", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.get("/api/mailforwarder/status", requireAuth, async (req, res) => {
  try {
    const r = await pyFetch("/api/mailforwarder/status");
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/mailforwarder/toggle", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/mailforwarder/toggle", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/mailforwarder/check-once", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/mailforwarder/check-once", { method: "POST" });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.get("/api/mailforwarder/settings", requireAuth, async (req, res) => {
  try {
    const r = await pyFetch("/api/mailforwarder/settings");
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/mailforwarder/settings", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/mailforwarder/settings", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/logout", (req, res) => {
  req.session.destroy((err) => {
    if (err) console.warn("[panel] session destroy:", err.message);
    res.clearCookie("anabot.sid", {
      path: "/",
      httpOnly: true,
      sameSite: "strict",
    });
    res.json({ ok: true });
  });
});

app.get("/api/joint", requireAuth, async (req, res) => {
  try {
    const r = await pyFetch("/api/joint");
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/refresh", requireAuth, async (req, res) => {
  try {
    const r = await pyFetch("/api/refresh", { method: "POST" });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/invite", requireAuth, async (req, res) => {
  try {
    const r = await pyFetch("/api/invite", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.get("/api/invite/recipients", requireAuth, async (req, res) => {
  try {
    const r = await pyFetch("/api/invite/recipients");
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/invite/revoke", requireAuth, requireAdmin, async (req, res) => {
  try {
    const r = await pyFetch("/api/invite/revoke", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/kick/preview", requireAuth, async (req, res) => {
  try {
    const r = await pyFetch("/api/kick/preview", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.post("/api/kick", requireAuth, async (req, res) => {
  try {
    const r = await pyFetch("/api/kick", {
      method: "POST",
      body: req.body || {},
    });
    const j = await r.json();
    res.status(r.status).json(j);
  } catch (e) {
    res.status(502).json({ error: String(e.message || e) });
  }
});

app.get("/dashboard", requireAuth, (req, res) => {
  res.sendFile(path.join(__dirname, "dashboard.html"));
});

app.get("/mailforwarder", requireAuth, (req, res) => {
  res.sendFile(path.join(__dirname, "mailforwarder.html"));
});

app.get("/admin", requireAuth, requireAdmin, (req, res) => {
  res.sendFile(path.join(__dirname, "admin.html"));
});

app.use(express.static(path.join(__dirname, "public")));

app.listen(PORT, BIND, () => {
  console.log(`[panel] http://${BIND}:${PORT}`);
});
