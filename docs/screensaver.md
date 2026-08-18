# A2687 custom screensaver images

The official app stores up to eight custom pictures in Anker's cloud and only
tells the charger which one to show. Listing, naming and downloading the files
is HTTP. Selecting the current picture is BLE (`0x021F` write, `0xE1` read).
`charger.screensaver_select_tlv` builds that write. How the 47-byte body was
recovered — and the guesses that did not work — is in [NOTES.md](../NOTES.md).

Verified against the CN app (`anker_power` 3.22.2) and this unit
(`ASHDJW7CF49200487`). The same paths exist in
[anker-solix-api](https://github.com/thomluther/anker-solix-api); that client
talks to the EU/COM hosts and never sets `x-encryption-info`. The CN host
accepts the same plaintext JSON if that header is omitted. The official app
wraps bodies in `algo_ecdh`; that wrapping is not required.

Do not commit account passwords, SMS codes, `auth_token` values, or a captured
`uid`. Pass them at runtime.

## Hosts

| region | host |
|---|---|
| CN | `https://aiot-api-cn.anker.com.cn` |
| COM | `https://ankerpower-api.anker.com` |
| EU | `https://ankerpower-api-eu.anker.com` |

Pick the host `passport/estimate_domain` returns for the account (`ab` = `CN`
here). All paths below are relative to that origin.

Common headers:

```
content-type: application/json
app-name: anker_power
app-version: 3.22.2
os-type: iOS
model-type: PHONE
country: CN
language: zh
```

After login, also send `uid`, `gtoken` (MD5 of `user_id`) and `x-auth-token`.

## Login (SMS)

Two calls. The send-code request is a bare JSON object. The login request
needs the same ECDH envelope the password login uses, or the server answers
`code: 10000` / `请求失败` with no further hint.

The server public key is a protocol constant (NIST P-256, uncompressed). It is
the same on the EU and COM login paths; CN accepted it as well:

```
04c5c00c4f8d1197cc7c3167c52bf7acb054d722f0ef08dcd7e0883236e0d72a
3868d9750cb47fa4619248f3d83f0f662671dadc6e2d31c2f41db0161651c7c076
```

Generate an ephemeral P-256 key. Put the uncompressed client point
(`04` + X + Y, hex) in `client_secret_info.public_key`. Password login also
AES-256-CBC-encrypts the password with the ECDH shared secret; SMS login does
not need that — the verify code travels in the clear inside the envelope.

**Send the SMS**

`POST /passport/phone_verification_code`

```json
{
  "phone_number": "<11-digit CN mobile>",
  "phone_code": "86",
  "kind": "login"
}
```

Missing `phone_number` or `kind` is a 400 (`field "…" is not set`). Success is
`code: 0` and an empty `data` object.

**Exchange the code for a token**

`POST /passport/phone_verification_login`

```json
{
  "ab": "CN",
  "client_secret_info": { "public_key": "<04… hex>" },
  "enc": 0,
  "phone_number": "<11-digit CN mobile>",
  "phone_code": "86",
  "verify_code": "<6 digits>",
  "kind": "login",
  "time_zone": 28800000,
  "transaction": "<unix ms as string>"
}
```

`data.auth_token` is the session token. `data.user_id` is the 40-character
account ID this repository already calls `--user-id` / `$ANKER_USER_ID`.
`gtoken` is `md5(user_id)`.

The code is single-use. A 400 names a missing field; `10000` after a complete
body usually means the code is gone or the envelope is missing.

Password login is `POST /passport/login` with `email` + encrypted `password`
instead of the phone fields; same envelope. One live token per account used to
kick the others; current app versions may keep several. Treat a new login as
able to bump the phone app anyway.

## List the pictures

**Custom uploads (the eight slots)**

`POST /mini_power/v1/app/style/get_manual_clock_screensavers`

```json
{ "sn": "<charger serial, e.g. ASHDJW…>" }
```

```json
{
  "code": 0,
  "data": {
    "total": 4,
    "list": [
      {
        "id": 24551,
        "seq": 1,
        "name": "FSF",
        "hash_code": "0x1da2ddca",
        "img_url": "https://edge-aiot-cn-pr.oss-cn-shenzhen.aliyuncs.com/…jpg?Expires=…&Signature=…",
        "short_url": "/anker_power/edge/screen_saver/…/….cropped_image.jpg"
      }
    ]
  }
}
```

| field | meaning |
|---|---|
| `id` | cloud picture id, unsigned. This is what the charger reports in `0xE1` |
| `seq` | 1-based slot in the app (1–8) |
| `name` | display title; empty string if never renamed |
| `hash_code` | file hash / CRC, 32-bit hex. Not the BLE id |
| `img_url` | signed JPEG, time-limited |
| `short_url` | durable path; turn it back into a signed URL with `get_url` |

On this firmware the four observed custom ids were `24551`, `45313`, `45314`,
`45317`. They are allocated on upload, not equal to `seq`. Gaps appear after a
replace or delete.

**Factory / clock themes**

`POST /mini_power/v1/app/style/get_clock_screensavers`

```json
{ "product_code": "A2687" }
```

A2687 returned `{ "category": [] }`. Other Prime chargers (e.g. A2345) return
named categories with `file_crc32` and `image_url` per theme. This unit has no
clock screensaver in the app, which matches the empty list.

## Refresh a signed URL

The `img_url` on the list is already signed. When it expires, either list
again or call one of:

`POST /mini_power/v1/app/style/get_screensaver_img_url`

```json
{ "sn": "<serial>", "id": 24551, "type": "manual" }
```

`POST /mini_power/v1/app/style/get_url`

```json
{ "sn": "<serial>", "short_url": "/anker_power/edge/screen_saver/…jpg" }
```

Both return `{ "img_url": "https://…" }`. CN objects live on Aliyun OSS
(`edge-aiot-cn-pr.oss-cn-shenzhen.aliyuncs.com`); EU examples in
anker-solix-api use S3. GET the JPEG with a normal HTTP client. No extra
Anker header is required on the object store.

Related writes, not needed just to download:

| path | body |
|---|---|
| `…/set_manual_clock_screensaver_name` | `{ "sn", "screensaver_id", "name" }` |
| `…/add_manual_clock_screensavers` | upload |
| `…/delete_manual_clock_screensavers` | delete |

There is no HTTP "make this the current picture". That is BLE.

## How this maps onto the charger

Realtime `0x0200` / `0x0300` TLV `0xE1` is a 10-byte struct. Bytes 2–3 (u16le)
**are the cloud `id`**. Bytes 0–1 are a flag word, usually `00 03` (`0x0300`)
or `80 03` (`0x0380`) — the high bit is not constant across pictures. Bytes
4–9 have stayed zero on this unit.

| `seq` | cloud `id` | `0xE1` bytes 2–3 (u16le) | example `0xE1` |
|---|---|---|---|
| 1 | 24551 | `0x5FE7` | `80 03 E7 5F 00 00 00 00 00 00` |
| 2 | 45313 | `0xB101` | |
| 3 | 45314 | `0xB102` | `80 03 02 B1 00 00 00 00 00 00` |
| 4 | 45317 | `0xB105` | `80 03 05 B1 00 00 00 00 00 00` |
| 5 | 45410 | `0xB162` | later upload named `cc` |

The official app's analytics event `AN_App_Custom_Picture_Change` carries
`value` as the 1-based `seq`. The charger does not store the title.
`get_device_setting` has no current-picture field. Snapshot `0xE0` is a
separate 4-byte value (`48 00 00 00` here) and does not change with the
picture.

Selecting a picture is a BLE write. Two official iOS traces (seq **1→2** at
18:47:42, **2→3** at 18:53:49) each sent one ATT Write Command of the same
shape:

```
FF09 49 00  03 00  0F  42 1F  <63-byte GCM>  <xor checksum>
```

Group `0x0F`, command `0x021F`, 73-byte frame. AES-GCM adds a 16-byte tag, so
the plaintext is **47 bytes**. The charger ACKs with a 30-byte `0x021F`
(4-byte plaintext `00 A1 01 31`). After 1→2, the next `0x0300` changed two
ciphertext bytes plus the tag — the `0xE1` cloud `id`. After 2→3 a live
`0x0200` read showed `0xE1` id `45314` (`0xB102`).

AES-GCM on this firmware reuses the session nonce, so same-session frames
share a keystream. Official `0x0300` starts `A1 01 31 A2 03 02 46 06 …`;
XOR that known prefix against a `0x0300` captured in the same PacketLogger
window as the select writes, then XOR the keystream onto each `0x021F`.
A `1→2→4→1→3` run recovered the 47-byte body on every jump (id and
`hash_code` matched). The official TLV is:

```
A1 01 21                         action marker
A3 02 01 03                      typed u8 screenSaverType = 3 (custom)
A4 05 04 <id u32le>              cloud picture id
A5 05 04 <hash_code u32le>       cloud hash_code
FD 11 00 "SmallChargingUrl"      0x00-tagged ASCII
FE 05 03 <epoch u32le>           typed timestamp
```

Slot `seq` is not sent. `charger.screensaver_select_tlv(id, hash_code)`
builds this. `0x0204` brightness still applies with only `A2` = account id;
`0x0027` `A3` password is optional.

## Minimal Python

Needs `cryptography`. Put the phone number and SMS code in the environment;
do not hard-code them.

```python
import hashlib, json, os, time, urllib.request
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

HOST = "https://aiot-api-cn.anker.com.cn"
SN = os.environ["ANKER_CHARGER_SN"]  # ASHDJW…
PHONE = os.environ["ANKER_PHONE"]
CODE = os.environ["ANKER_SMS_CODE"]
PUB = bytes.fromhex(
    "04c5c00c4f8d1197cc7c3167c52bf7acb054d722f0ef08dcd7e0883236e0d72a"
    "3868d9750cb47fa4619248f3d83f0f662671dadc6e2d31c2f41db0161651c7c076"
)

def post(path, body, token=None, uid=None):
    headers = {
        "content-type": "application/json",
        "app-name": "anker_power",
        "country": "CN",
        "user-agent": "ktor-client",
    }
    if token:
        headers["x-auth-token"] = token
        headers["uid"] = uid
        headers["gtoken"] = hashlib.md5(uid.encode()).hexdigest()
    req = urllib.request.Request(
        HOST + path,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.load(resp)

# 1) send SMS once: post("/passport/phone_verification_code",
#        {"phone_number": PHONE, "phone_code": "86", "kind": "login"})

priv = ec.generate_private_key(ec.SECP256R1())
client_pub = priv.public_key().public_bytes(
    Encoding.X962, PublicFormat.UncompressedPoint
).hex()
login = post("/passport/phone_verification_login", {
    "ab": "CN",
    "client_secret_info": {"public_key": client_pub},
    "enc": 0,
    "phone_number": PHONE,
    "phone_code": "86",
    "verify_code": CODE,
    "kind": "login",
    "time_zone": 8 * 3600 * 1000,
    "transaction": str(int(time.time() * 1000)),
})["data"]

listing = post(
    "/mini_power/v1/app/style/get_manual_clock_screensavers",
    {"sn": SN},
    token=login["auth_token"],
    uid=login["user_id"],
)
for pic in listing["data"]["list"]:
    print(pic["seq"], pic["id"], pic["name"] or "-", pic["img_url"])
```

`GET` each `img_url` to save the JPEG. Re-run the list (or `get_screensaver_img_url`)
when a signature expires.
