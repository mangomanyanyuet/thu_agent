"""账号体系：注册 / 登录 / 令牌校验。

⚠ 定位说明（务必读完再用于生产）
------------------------------
这是**演示级**实现，目标是把「谁在看谁的数据」这件事立起来，
而不是替代生产环境的身份认证。上线前至少还要补齐：

1. **必须 HTTPS**：密码与 token 走明文 HTTP 等于裸奔。本仓库的
   `certs/` 是自签证书，浏览器不信任，仅内网调试可用。
2. **密码哈希**：这里用 `hashlib.pbkdf2_hmac`（stdlib，无额外依赖）。
   生产应换成 bcrypt / argon2 —— pbkdf2 抗 GPU 暴破能力明显弱于后者。
3. **无登录失败限速**：目前可无限次试密码。生产需加 IP + 账号维度的
   限速与锁定。
4. **无密码强度策略、无邮箱/手机验证、无找回流程**。
5. **数据隔离只做到 user_id 维度**：接口仍信任调用方传来的 user_id。
   严格做法应是「token -> user_id 由服务端解析，不接受客户端指定」。
   见 `enforce_user_match()`。

心理数据属敏感个人信息，账号体系是必要条件而非充分条件。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta

from config import get_settings
from storage import _conn, tx

# pbkdf2 迭代次数。CPU 上约 100ms/次，是安全与响应时间的折中。
PBKDF2_ROUNDS = 200_000
TOKEN_TTL_HOURS = 24
MIN_PASSWORD_LEN = 6


class AuthError(Exception):
    """可对外展示的鉴权错误（消息安全，不含内部细节）。"""


# ------------------------------------------------------------------ 密码
def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()


def _verify_password(password: str, salt: str, expected: str) -> bool:
    # 定长比较，避免时序侧信道
    return hmac.compare_digest(_hash_password(password, salt), expected)


def _validate_credentials(username: str, password: str) -> None:
    u = (username or "").strip()
    if len(u) < 2 or len(u) > 32:
        raise AuthError("用户名需 2-32 个字符")
    if not u.isalnum() and not all(c.isalnum() or c in "_-." for c in u):
        raise AuthError("用户名只能包含字母、数字、下划线、连字符和点")
    if len(password or "") < MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    if len(password or "") > 128:
        raise AuthError("密码过长")


# ------------------------------------------------------------------ 账号
def register(username: str, password: str, display_name: str = "") -> dict:
    """注册并直接返回可用 token。"""
    _validate_credentials(username, password)
    u = username.strip()
    salt = secrets.token_hex(16)
    pwd_hash = _hash_password(password, salt)
    now = datetime.now().isoformat(timespec="seconds")

    try:
        with tx() as c:
            c.execute(
                "INSERT INTO app_user(username,password_hash,salt,display_name,created_at)"
                " VALUES(?,?,?,?,?)",
                (u, pwd_hash, salt, (display_name or u).strip(), now),
            )
    except sqlite3.IntegrityError as exc:
        raise AuthError("该用户名已被注册") from exc

    user_id = f"u_{u}"
    return issue_token(user_id, u)


def login(username: str, password: str) -> dict:
    u = (username or "").strip()
    if not u or not password:
        raise AuthError("请输入用户名和密码")

    with tx() as c:
        row = c.execute(
            "SELECT username, password_hash, salt FROM app_user WHERE username=?", (u,)
        ).fetchone()

    # 用户不存在时也走一次哈希，避免通过响应时间枚举用户名
    if row is None:
        _hash_password(password, "dummy-salt-for-timing")
        raise AuthError("用户名或密码错误")

    if not _verify_password(password, row["salt"], row["password_hash"]):
        raise AuthError("用户名或密码错误")

    now = datetime.now().isoformat(timespec="seconds")
    with tx() as c:
        c.execute("UPDATE app_user SET last_login=? WHERE username=?", (now, u))

    return issue_token(f"u_{u}", u)


# ------------------------------------------------------------------ 令牌
def issue_token(user_id: str, username: str) -> dict:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    exp = now + timedelta(hours=TOKEN_TTL_HOURS)
    with tx() as c:
        c.execute(
            "INSERT INTO auth_token(token,user_id,username,created_at,expires_at)"
            " VALUES(?,?,?,?,?)",
            (token, user_id, username,
             now.isoformat(timespec="seconds"), exp.isoformat(timespec="seconds")),
        )
    return {
        "token": token,
        "user_id": user_id,
        "username": username,
        "expires_at": exp.isoformat(timespec="seconds"),
    }


def verify_token(token: str) -> dict | None:
    """校验 token；过期返回 None 并顺手清理。"""
    if not token:
        return None
    with tx() as c:
        row = c.execute(
            "SELECT user_id, username, expires_at FROM auth_token WHERE token=?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now():
            c.execute("DELETE FROM auth_token WHERE token=?", (token,))
            return None
    return {"user_id": row["user_id"], "username": row["username"]}


def revoke_token(token: str) -> None:
    if not token:
        return
    with tx() as c:
        c.execute("DELETE FROM auth_token WHERE token=?", (token,))


def purge_expired_tokens() -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with tx() as c:
        return c.execute("DELETE FROM auth_token WHERE expires_at<?", (now,)).rowcount


def enforce_user_match(session: dict, user_id: str) -> None:
    """校验业务接口传入的 user_id 与登录身份一致。

    不校验的话，登录 A 账号就可以读写 B 的数据（越权）。
    """
    if not session:
        raise AuthError("未登录")
    if user_id and user_id != session["user_id"]:
        raise AuthError("无权访问该用户的数据")


# ------------------------------------------------------------------ 初始化
def ensure_default_account() -> str | None:
    """首次启动时建一个演示账号，避免装完没法登录。

    账号密码写在 config 里，且**只在账号表为空时创建** —— 已经有人注册过
    就不再创建，防止重启后意外复活一个默认账号。
    """
    s = get_settings()
    if not s.demo_username or not s.demo_password:
        return None
    with tx() as c:
        exists = c.execute("SELECT 1 FROM app_user LIMIT 1").fetchone()
        if exists:
            return None
    register(s.demo_username, s.demo_password, s.demo_display_name)
    return s.demo_username


def user_count() -> int:
    with tx() as c:
        return c.execute("SELECT COUNT(*) n FROM app_user").fetchone()["n"]


def default_account_untouched() -> dict | None:
    """演示账号是否仍是"全新"状态（自动创建且从未登录过）。

    是则返回其初始账号密码供登录页提示；否则返回 None。
    """
    s = get_settings()
    if not s.demo_username or not s.demo_password:
        return None
    with tx() as c:
        row = c.execute(
            "SELECT last_login FROM app_user WHERE username=?", (s.demo_username,)
        ).fetchone()
    if row is None or (row["last_login"] or "").strip():
        return None
    return {"username": s.demo_username, "password": s.demo_password}
