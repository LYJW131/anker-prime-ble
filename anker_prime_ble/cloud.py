"""Anker CN/COM/EU cloud: login, screensaver list, register, OSS token.

Listing and naming custom covers is HTTP. Selecting the current picture is BLE.
This module talks to the same hosts the official app uses. The CN host accepts
plaintext JSON if `x-encryption-info` is omitted — the official app wraps
bodies in `algo_ecdh`, but that wrapping is not required.

Credentials stay in the environment. Do not commit `auth_token`, `user_id`,
passwords, or SMS codes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import string
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

log = logging.getLogger("anker.cloud")

HOSTS = {
    "CN": "https://aiot-api-cn.anker.com.cn",
    "COM": "https://ankerpower-api.anker.com",
    "EU": "https://ankerpower-api-eu.anker.com",
}

# Static NIST P-256 point used to wrap the login password (and SMS envelope).
# Same value on EU and COM; CN accepted it as well.
SERVER_PUBLIC_KEY = bytes.fromhex(
    "04c5c00c4f8d1197cc7c3167c52bf7acb054d722f0ef08dcd7e0883236e0d72a"
    "3868d9750cb47fa4619248f3d83f0f662671dadc6e2d31c2f41db0161651c7c076"
)

APP_NAME = "anker_power"
APP_VERSION = "3.22.2"

PATH_SMS_SEND = "/passport/phone_verification_code"
PATH_SMS_LOGIN = "/passport/phone_verification_login"
PATH_PASSWORD_LOGIN = "/passport/login"
PATH_LIST_MANUAL = "/mini_power/v1/app/style/get_manual_clock_screensavers"
PATH_LIST_STOCK = "/mini_power/v1/app/style/get_clock_screensavers"
PATH_GET_URL = "/mini_power/v1/app/style/get_url"
PATH_GET_IMG_URL = "/mini_power/v1/app/style/get_screensaver_img_url"
PATH_ADD_MANUAL = "/mini_power/v1/app/style/add_manual_clock_screensavers"
PATH_SET_NAME = "/mini_power/v1/app/style/set_manual_clock_screensaver_name"
PATH_DELETE_MANUAL = "/mini_power/v1/app/style/delete_manual_clock_screensavers"
PATH_UP_TOKEN = "/app/cloudstor/get_app_up_token_general"

# Live-probed on CN 2026-08-18. `type` is a required integer; `file_name` is
# required once type is set. Types 4, 6 and 7 return a signed PUT URL.
# 6 and 7 point at the edge-aiot host the official covers already use.
# Response keys are snake_case: `uptoken`, `key_prefix`.
SCREENSAVER_UP_TOKEN_TYPE = 6
_TOKEN_TYPE_CANDIDATES = (6, 7, 4)


class CloudError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.code = code
        self.body = body


@dataclass
class Picture:
    id: int
    seq: int
    name: str
    hash_code: int
    img_url: str
    short_url: str
    raw: dict

    def hash_hex(self) -> str:
        return format_hash_code(self.hash_code)


def parse_hash_code(value: Any) -> int:
    """Cloud `hash_code` is a 32-bit hex string such as `0x1da2ddca`."""
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = str(value).strip().lower()
    if text.startswith("0x"):
        return int(text, 16) & 0xFFFFFFFF
    if text.isdigit():
        return int(text) & 0xFFFFFFFF
    raise ValueError(f"unrecognised hash_code: {value!r}")


def format_hash_code(value: int) -> str:
    return f"0x{value & 0xFFFFFFFF:08x}"


def gtoken(user_id: str) -> str:
    return hashlib.md5(user_id.encode("ascii")).hexdigest()


def random_object_name() -> str:
    """Official OSS keys end in `<16 alnum>.cropped_image.jpg`."""
    alphabet = string.ascii_letters + string.digits
    stem = "".join(random.choice(alphabet) for _ in range(16))
    return f"{stem}.cropped_image.jpg"


def _base_headers() -> dict[str, str]:
    return {
        "content-type": "application/json",
        "app-name": APP_NAME,
        "app-version": APP_VERSION,
        "os-type": "iOS",
        "model-type": "PHONE",
        "country": "CN",
        "language": "zh",
        "user-agent": "ktor-client",
    }


class CloudSession:
    """One logged-in Anker cloud session. Tokens are never written to disk."""

    def __init__(self, host: Optional[str] = None, ab: str = "CN") -> None:
        self.ab = ab.upper()
        self.host = (host or os.environ.get("ANKER_HOST") or HOSTS.get(self.ab, HOSTS["CN"])).rstrip("/")
        self.auth_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.login_data: dict = {}

    # -- HTTP ----------------------------------------------------------------

    def _headers(self, auth: bool) -> dict[str, str]:
        headers = _base_headers()
        headers["country"] = "CN" if self.ab == "CN" else self.ab
        if auth:
            if not self.auth_token or not self.user_id:
                raise CloudError("not logged in")
            headers["x-auth-token"] = self.auth_token
            headers["uid"] = self.user_id
            headers["gtoken"] = gtoken(self.user_id)
        return headers

    def post(self, path: str, body: dict, *, auth: bool = False) -> dict:
        url = path if path.startswith("http") else self.host + path
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=self._headers(auth), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            status = exc.code
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise CloudError(f"{path} HTTP {status}: not JSON: {raw[:240]}", body=raw) from exc
        if not isinstance(parsed, dict):
            raise CloudError(f"{path} HTTP {status}: unexpected body", body=parsed)
        code = parsed.get("code")
        if status >= 400 or (code not in (None, 0)):
            raise CloudError(
                f"{path} HTTP {status} code={code} msg={parsed.get('msg')!r}",
                code=code if isinstance(code, int) else status,
                body=parsed,
            )
        return parsed

    # -- login ---------------------------------------------------------------

    def _ecdh(self) -> tuple[str, bytes]:
        priv = ec.generate_private_key(ec.SECP256R1())
        server = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), SERVER_PUBLIC_KEY)
        shared = priv.exchange(ec.ECDH(), server)
        client_pub = priv.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint).hex()
        return client_pub, shared

    def _encrypt_password(self, password: str, shared: bytes) -> str:
        # AES-256-CBC, key = ECDH shared (32 bytes), IV = shared[:16], PKCS7.
        import base64

        padder = PKCS7(128).padder()
        padded = padder.update(password.encode("utf-8")) + padder.finalize()
        encryptor = Cipher(algorithms.AES(shared), modes.CBC(shared[:16])).encryptor()
        return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")

    def _accept_login(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise CloudError("login data is not an object — the body may still be encrypted")
        token = data.get("auth_token") or data.get("token")
        uid = data.get("user_id") or data.get("uid")
        if not token or not uid:
            raise CloudError("login succeeded but no auth_token/user_id", body=data)
        self.auth_token = str(token)
        self.user_id = str(uid)
        self.login_data = data
        log.info("logged in uid=%s…%s", self.user_id[:6], self.user_id[-4:])
        return data

    def login_password(self, email: str, password: str) -> dict:
        client_pub, shared = self._ecdh()
        body = {
            "ab": self.ab,
            "client_secret_info": {"public_key": client_pub},
            "enc": 0,
            "email": email,
            "password": self._encrypt_password(password, shared),
            "time_zone": 8 * 3600 * 1000,
            "transaction": str(int(time.time() * 1000)),
        }
        resp = self.post(PATH_PASSWORD_LOGIN, body, auth=False)
        return self._accept_login(resp.get("data") or {})

    def send_sms(self, phone: str, phone_code: str = "86") -> dict:
        return self.post(
            PATH_SMS_SEND,
            {"phone_number": phone, "phone_code": phone_code, "kind": "login"},
            auth=False,
        )

    def login_sms(self, phone: str, verify_code: str, phone_code: str = "86") -> dict:
        client_pub, _shared = self._ecdh()
        body = {
            "ab": self.ab,
            "client_secret_info": {"public_key": client_pub},
            "enc": 0,
            "phone_number": phone,
            "phone_code": phone_code,
            "verify_code": verify_code,
            "kind": "login",
            "time_zone": 8 * 3600 * 1000,
            "transaction": str(int(time.time() * 1000)),
        }
        resp = self.post(PATH_SMS_LOGIN, body, auth=False)
        return self._accept_login(resp.get("data") or {})

    def use_token(self, auth_token: str, user_id: str) -> None:
        self.auth_token = auth_token
        self.user_id = user_id

    # -- screensavers --------------------------------------------------------

    def list_manual(self, sn: str) -> list[Picture]:
        data = (self.post(PATH_LIST_MANUAL, {"sn": sn}, auth=True).get("data") or {})
        pictures = []
        for item in data.get("list") or []:
            pictures.append(
                Picture(
                    id=int(item["id"]),
                    seq=int(item.get("seq") or 0),
                    name=str(item.get("name") or ""),
                    hash_code=parse_hash_code(item.get("hash_code")),
                    img_url=str(item.get("img_url") or ""),
                    short_url=str(item.get("short_url") or ""),
                    raw=item,
                )
            )
        return pictures

    def list_stock(self, product_code: str = "A2687") -> dict:
        return self.post(PATH_LIST_STOCK, {"product_code": product_code}, auth=True).get("data") or {}

    def signed_url(self, sn: str, picture: Picture) -> str:
        if picture.img_url:
            return picture.img_url
        if picture.short_url:
            data = self.post(PATH_GET_URL, {"sn": sn, "short_url": picture.short_url}, auth=True)
            return str((data.get("data") or {}).get("img_url") or "")
        data = self.post(
            PATH_GET_IMG_URL,
            {"sn": sn, "id": picture.id, "type": "manual"},
            auth=True,
        )
        return str((data.get("data") or {}).get("img_url") or "")

    def add_manual(self, sn: str, img_url: str, hash_code: int | str) -> dict:
        body = {
            "sn": sn,
            "img_url": img_url,
            "hash_code": hash_code if isinstance(hash_code, str) else format_hash_code(hash_code),
        }
        return self.post(PATH_ADD_MANUAL, body, auth=True)

    def set_name(self, sn: str, screensaver_id: int, name: str) -> dict:
        return self.post(
            PATH_SET_NAME,
            {"sn": sn, "screensaver_id": screensaver_id, "name": name},
            auth=True,
        )

    def delete_manual(self, picture_id: int) -> dict:
        return self.post(PATH_DELETE_MANUAL, {"id": picture_id}, auth=True)

    # -- OSS token / upload --------------------------------------------------

    def get_up_token(
        self,
        extra: Optional[dict] = None,
        *,
        file_name: Optional[str] = None,
        token_type: Optional[int] = None,
    ) -> dict:
        """Ask for an OSS upload token.

        Live CN schema: `{"type": <int>, "file_name": "<name>"}`. Types 4/6/7
        succeed; 6 and 7 return an `edge-aiot…` signed PUT URL. The body is
        `{"uptoken": "https://…?Expires=…", "key_prefix": "/anker_power/…"`.
        """
        name = file_name or random_object_name()
        if extra is not None:
            body = dict(extra)
            body.setdefault("file_name", name)
            data = self.post(PATH_UP_TOKEN, body, auth=True).get("data") or {}
            if not isinstance(data, dict):
                raise CloudError("up-token data is not an object", body=data)
            return data

        errors: list[str] = []
        types = (token_type,) if token_type is not None else _TOKEN_TYPE_CANDIDATES
        for guessed in types:
            body = {"type": guessed, "file_name": name}
            try:
                data = self.post(PATH_UP_TOKEN, body, auth=True).get("data") or {}
            except CloudError as exc:
                errors.append(f"{body}: {exc}")
                continue
            if isinstance(data, dict) and (data.get("uptoken") or data.get("upToken")):
                data.setdefault("_type", guessed)
                data.setdefault("_file_name", name)
                log.info("up-token type=%s keys=%s", guessed, list(data))
                return data
            errors.append(f"{body}: empty data {data!r}")
        raise CloudError("get_app_up_token_general rejected every type:\n  " + "\n  ".join(errors))

    def upload_jpeg(self, jpeg: bytes, token: dict, object_name: Optional[str] = None) -> str:
        """PUT the JPEG to the signed `uptoken` URL. Returns `key_prefix` (short path)."""
        up_token = token.get("uptoken") or token.get("upToken") or token.get("token")
        key_prefix = (
            token.get("key_prefix")
            or token.get("keyPrefix")
            or token.get("dirName")
            or ""
        )
        if isinstance(up_token, str) and up_token.startswith("http"):
            _http_put(up_token, jpeg)
            if key_prefix:
                return str(key_prefix)
            parsed = urlparse(up_token)
            return parsed.path or up_token.split("?", 1)[0]

        object_name = object_name or token.get("_file_name") or random_object_name()
        prefix = str(key_prefix).lstrip("/")
        object_key = f"{prefix.rstrip('/')}/{object_name}" if prefix else str(object_name)
        host = token.get("host") or token.get("endpoint") or token.get("upload_url") or token.get("url")
        if token.get("policy") and (token.get("signature") or token.get("OSSAccessKeyId")):
            return _oss_post_form(token, object_key, jpeg)
        if host:
            url = str(host).rstrip("/") + "/" + object_key
            return _http_put(url, jpeg, token=str(up_token) if up_token else None)
        raise CloudError(
            "do not know how to upload with this token. keys=" + ",".join(sorted(token)),
            body={k: _redact(token[k]) for k in token},
        )


def download_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"user-agent": "anker-prime-ble"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def _load_cache(path: str) -> Optional[tuple[str, str]]:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    token = data.get("auth_token")
    uid = data.get("user_id")
    if token and uid:
        return str(token), str(uid)
    return None


def _save_cache(path: str, session: CloudSession) -> None:
    if not session.auth_token or not session.user_id:
        return
    target = Path(path)
    target.write_text(json.dumps({"auth_token": session.auth_token, "user_id": session.user_id}))
    target.chmod(0o600)


def login_from_env(
    *,
    phone: Optional[str] = None,
    password: Optional[str] = None,
    sms_code: Optional[str] = None,
    token: Optional[str] = None,
    user_id: Optional[str] = None,
    host: Optional[str] = None,
    ab: Optional[str] = None,
    cache: Optional[str] = None,
) -> CloudSession:
    """Build a session from arguments or the ANKER_* environment.

    Passport login is rate-limited (`code 100028`). Prefer
    `ANKER_AUTH_TOKEN`+`ANKER_USER_ID`, or `ANKER_AUTH_CACHE` pointing at a
    0600 JSON file this helper will refresh after a successful login.
    """
    session = CloudSession(
        host=host or os.environ.get("ANKER_HOST"),
        ab=ab or os.environ.get("ANKER_AB") or "CN",
    )
    cache = cache or os.environ.get("ANKER_AUTH_CACHE")
    token = token or os.environ.get("ANKER_AUTH_TOKEN")
    user_id = user_id or os.environ.get("ANKER_USER_ID")
    if token and user_id:
        session.use_token(token, user_id)
        return session
    if cache:
        cached = _load_cache(cache)
        if cached:
            session.use_token(*cached)
            return session

    phone = phone or os.environ.get("ANKER_PHONE")
    password = password or os.environ.get("ANKER_PASSWORD")
    sms_code = sms_code or os.environ.get("ANKER_SMS_CODE")
    if not phone:
        raise CloudError("set ANKER_PHONE (or pass --phone)")
    try:
        if password:
            session.login_password(phone, password)
        elif sms_code:
            session.login_sms(phone, sms_code)
        else:
            raise CloudError("set ANKER_PASSWORD, or ANKER_SMS_CODE, or ANKER_AUTH_TOKEN+ANKER_USER_ID")
    except CloudError as exc:
        if exc.code == 100028:
            raise CloudError(
                "passport login rate-limited (100028). Reuse ANKER_AUTH_TOKEN "
                "or ANKER_AUTH_CACHE instead of logging in again.",
                code=exc.code,
                body=exc.body,
            ) from exc
        raise
    if cache:
        _save_cache(cache, session)
    return session


def _redact(value: Any) -> Any:
    if not isinstance(value, str) or len(value) < 16:
        return value
    return value[:4] + "…" + value[-4:]


def _http_put(url: str, data: bytes, token: Optional[str] = None, content_type: Optional[str] = None) -> str:
    # Pre-signed OSS URLs often bind Content-Type into the signature. Sending
    # a type the signer did not include makes the PUT 403, so default to none.
    headers = {}
    if content_type:
        headers["content-type"] = content_type
    if token:
        headers["authorization"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise CloudError(f"OSS PUT {urlparse(url).path} HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    parsed = urlparse(url)
    return parsed.path or url


def _oss_post_form(token: dict, object_key: str, data: bytes) -> str:
    import uuid
    from email.generator import BytesGenerator
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from io import BytesIO

    host = str(token.get("host") or token.get("endpoint") or "")
    if not host:
        raise CloudError("OSS form upload needs host", body=token)
    fields = {
        "key": object_key,
        "policy": token.get("policy"),
        "OSSAccessKeyId": token.get("OSSAccessKeyId") or token.get("accessKeyId"),
        "signature": token.get("signature"),
        "success_action_status": "200",
    }
    if token.get("securityToken") or token.get("x-oss-security-token"):
        fields["x-oss-security-token"] = token.get("securityToken") or token.get("x-oss-security-token")

    boundary = uuid.uuid4().hex
    form = MIMEMultipart("form-data", boundary=boundary)
    for name, value in fields.items():
        if value is None:
            continue
        part = MIMEText(str(value))
        part.add_header("Content-Disposition", "form-data", name=name)
        form.attach(part)
    file_part = MIMEApplication(data, _subtype="jpeg")
    file_part.add_header("Content-Disposition", "form-data", name="file", filename=object_key.rsplit("/", 1)[-1])
    form.attach(file_part)

    buf = BytesIO()
    # BytesGenerator adds a header we do not want; take the payload only.
    gen = BytesGenerator(buf, mangle_from_=False)
    gen.flatten(form)
    body = buf.getvalue()
    header_end = body.find(b"\r\n\r\n")
    payload = body[header_end + 4 :] if header_end >= 0 else body
    req = urllib.request.Request(
        host,
        data=payload,
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise CloudError(f"OSS POST HTTP {exc.code}: {exc.read()[:300]!r}") from exc
    return "/" + object_key.lstrip("/")
