# couchy-agent

A ComfyUI custom node pack for [Couchy](https://github.com/getcouchy/couchy). It registers no
nodes. It keeps each Couchy tenant's files on the server in a folder of its own, and each
person's files in a folder inside that:

```
input/couchy/<namespace>/<owner>/<id>.png        uploads
output/couchy/<namespace>/<owner>/<runId>/...    outputs
```

A **tenant** is one Couchy API's workspace: prod Lab, pre-prod Lab, a later team. Its
`<namespace>` is a random `ns_...` id the agent makes when the tenant pairs. `<owner>` is a
Couchy user id, or `lab`. There are no names in paths.

The agent is mechanism only. Who owns a file, who may see it and who may delete it are
decided by the Couchy API, which is the only door people use. The agent makes sure each
tenant reaches only its own namespace, and that nothing reaches a namespace without its key.

## Install

Clone it into ComfyUI's `custom_nodes/` and restart ComfyUI:

```sh
git clone https://github.com/getcouchy/couchy-agent custom_nodes/couchy-agent
```

It needs Python 3.10+, macOS or Linux, and nothing beyond what ComfyUI installs. It was
checked against ComfyUI 0.44.0.

## Pairing

On a start with no code waiting, the agent writes a one-time pairing code to ComfyUI's log
and to `user/couchy-agent/pairing-code`:

```
couchy-agent: pairing code 8GMJ-4630-R85P (valid 24 hours, also in .../user/couchy-agent/pairing-code)
```

In Couchy, an Admin opens **Settings › Servers ›** the server **› Storage › Pair** and enters
it. Couchy gets back a namespace and a key, and keeps the key encrypted.

To print a fresh code (it replaces any waiting one), run this from the pack's folder, as the
account ComfyUI runs as:

```sh
cd custom_nodes/couchy-agent
sudo -u comfy python -m couchy_agent pair
# --user-directory <dir> if ComfyUI was started with one
```

`python -m couchy_agent keys` lists the namespaces and key ids, never the keys.

Codes are 12 characters (about 60 bits; case, dashes and spaces don't matter), stored
hashed, single use and valid 24 hours. Five wrong codes lock that client out for 15 minutes,
and all clients together get 30 tries a minute.

A server can pair with more than one tenant, each with its own namespace. The agent keeps
only each key's hash, bound to its namespace, a mode and a models grant, in
`user/couchy-agent/keys.json`.

A code grants `models: read`. To pair a Couchy that may also change this server's models
(pull and delete, in a later release), print the code with the flag, on the host:

```sh
sudo -u comfy python -m couchy_agent pair --models write
```

No route can raise a key's models grant; only a code printed this way gives `write`.

## Keys

Every `/couchy/*` call except pairing carries the key:

```
Authorization: Couchy-Agent cak_...
```

If a token proxy in front of ComfyUI already uses `Authorization` (Couchy sends
`Authorization: Bearer <server token>`), send the key as `X-Couchy-Agent-Key: cak_...`
instead.

A key is `rw` or `ro` on its own namespace and nothing else.

- **`rw`** comes from pairing. It can do everything below.
- **`ro`** can report usage, list files and `/view` them, but deleting, adopting, moving
  and minting keys answer 403. Pre-prod runs on a copy of prod's database, so its rows point
  into prod's namespace; prod mints an `ro` key for pre-prod with `POST /couchy/keys`, and
  pre-prod pairs for its own `rw` namespace as well. Pre-prod's key then can't delete prod's
  files, whatever the API does.

Each key also has a **models grant**, separate from its mode, because model files aren't
namespaced: every tenant on the server shares `models/`, and deleting a checkpoint breaks
every tenant's runs.

| Grant | Can |
| --- | --- |
| `none` | Nothing under `/couchy/models` (403) |
| `read` | The inventory, headers and space. Every pairing gets it unless the code says otherwise |
| `write` | Also pull, delete and serve copies to peers (in a later release). Only `pair --models write` gives it |

A key minted with `POST /couchy/keys` gets a models grant no higher than its minter's
(`read` by default, or the minter's own when that's lower), so prod can mint pre-prod an
`ro` key with `models: read`. Keys from before the grant read as `read`, and the agent writes
that into `keys.json` on start.

## Routes

All routes are also served under `/api/`, like ComfyUI's own. `folder` is `input` or
`output`; `subfolder` is relative to that folder, as ComfyUI's `/view` takes it.

| Route | Does |
| --- | --- |
| `POST /couchy/pair {code, label}` | No key. Spends a code; answers `{namespace, key, keyId, mode: "rw", models}`. 403 for a wrong, used or expired code, 429 when locked out |
| `GET /couchy/usage` | `{namespace, folders: {input, output: {count, bytes, owners: {<owner>: {count, bytes}}}}, permissions: "ok" \| "open"}`. Never file names |
| `GET /couchy/files?folder&owner[&after][&limit]` | A page (at most 1000) of one owner's files: `{files: [{subfolder, filename, size, mtime}], total: {count, bytes}, next?}`. Pass `next` as `after` |
| `POST /couchy/files/delete {folder, files: [{subfolder, filename}]}` | Answers `{deleted, missing, refused}`, each a list |
| `POST /couchy/files/delete {folder, owner}` | Deletes all of one owner's files; answers `{deleted, bytes}` |
| `POST /couchy/files/delete {folder, olderThan}` | Deletes the namespace's files last changed more than `olderThan` seconds ago; answers `{deleted, bytes}` |
| `POST /couchy/files/adopt {from: {folder, subfolder, filename}, owner, runId}` | Moves a file from `output` or `temp`, outside `couchy/`, into `output/couchy/<ns>/<owner>/<runId>/`. Answers `{state: "adopted", to: {folder, subfolder, filename}}`, `{state: "missing"}` (gone, as on a second adopt of the same file) or `{state: "refused"}` (another namespace, outside the folder, a symlink). A taken name gets `_1`, `_2` ... |
| `POST /couchy/files/move-owner {from, to}` | Re-homes one owner's files in `input` and `output`; answers `{input: {moved}, output: {moved}}` |
| `GET /couchy/keys` | The namespace's key ids, modes, models grants and labels |
| `POST /couchy/keys {mode: "ro" \| "rw", label, models?}` | Mints another key for the caller's namespace (`rw` caller only). `models` is `none`, `read` or `write`, no higher than the caller's (403 otherwise). Answers `{id, key, namespace, mode, models}` once; the key isn't kept |
| `POST /couchy/keys/revoke {id}` | Revokes another key of the namespace (`rw` caller only; not itself). Answers `{revoked}` |

The path rules are those of couchyforms-helper's `files.py`: every path is resolved,
symlinks included, and must stay inside its folder; symlinks are never listed; folders are
never deleted. On top of that, inside `couchy/` only the key's namespace is reachable.
Owners and run ids are letters, digits, `_` and `-`.

## Models

The models routes are read-only for now: what's on this host's disks, what a `.safetensors`
file's header says, and how much room each volume has. The agent is mechanism only here too:
it never decides what to keep, load or evict.

### Locations and shares

A file lives in a **location**: `host` is ComfyUI's own models folders (as `folder_paths`
has them, `extra_model_paths.yaml` included), and a **share** is a model root mounted on this
host, such as a NAS over CIFS or NFS, laid out like ComfyUI's `models/` (`checkpoints/`,
`loras/`, ...). Set the shares on the host, as the account ComfyUI runs as:

```sh
sudo -u comfy python -m couchy_agent config --share nas-hot=/mnt/models-hot --share nas-cold=/mnt/models-cold
sudo -u comfy python -m couchy_agent config                 # show them
sudo -u comfy python -m couchy_agent config --clear-shares
```

`--share` repeats, and each call replaces the shares set before. They're kept in
`user/couchy-agent/config.json` (`0600`), and the running agent picks a change up on its
next scan. A share name is lowercase letters, digits, `-` and `_`, and never `host`. An
`extra_model_paths.yaml` folder inside a share is listed under the share, not twice.

`folder` is a ComfyUI models folder type as `folder_paths` names it (`checkpoints`, `loras`,
`diffusion_models`, `text_encoders`, `vae`, ...), and `path` is relative to it, with `/`.
A type with two directories (`text_encoders` is `text_encoders/` and `clip/`) lists the
first match of a path, as ComfyUI loads it. Below a folder, symlinks are never listed,
hashed or read, and nothing that resolves outside it is reached. A folder that is itself a
symlink (`models/checkpoints` pointing at a bigger disk) is fine.

### Routes

All need a key whose models grant is `read` or `write`, and are also served under `/api/`.

| Route | Does |
| --- | --- |
| `GET /couchy/models?[folder][&location][&after][&limit]` | A page (at most 1000) of model files, every folder and location unless narrowed: `{files: [{location, folder, path, size, mtime, sha256, verifiedAt}], next?}`. `sha256` and `verifiedAt` are null until the file is hashed. Pass `next` as `after` |
| `GET /couchy/models/header?folder&path[&location]` | A `.safetensors` file's header, read from its first bytes: `{metadata, tensors: [{name, dtype, shape}], tensorCount, truncated}`. `location` defaults to `host`. At most 2000 tensors are listed. 422 for any other format and for a header over 1 MB; 404 for a missing file or a symlink |
| `GET /couchy/models/space` | Each volume that holds a models folder: `{volumes: [{location, folders, freeBytes, totalBytes}]}` |

A listing is the inventory as of the last scan: the host is scanned every minute and shares
every five, so a NAS isn't walked on every page.

### Hashing

Hashing a 23 GB checkpoint takes minutes, so it never runs on a request. A background thread
hashes new and changed files one at a time, at low priority (a niced thread on Linux), across
`host` and every share, and keeps `{location, folder, path, size, mtime} → {sha256,
verifiedAt}` in `user/couchy-agent/models-index.json` (`0600`). After a restart only files
whose size or mtime changed are hashed again.

Each file is **re-verified weekly** by hashing it again. A match sets `verifiedAt`. A mismatch
(the bytes changed under the same size and mtime) clears `sha256` and `verifiedAt`, logs
`model verify mismatch` as a warning, and leaves the file unhashed until it changes. A share
that turns up empty or missing (a mount that failed) keeps its hashes until it's back.

### Logging

The models lines are JSON, one object per line on ComfyUI's console, each with
`service: "couchy-agent"`, `version` and `server` (the host name). They name a file by its
location, folder and the first 12 hex characters of its SHA-256 (`sha`), with `bytes` and
`ms`, and never by its path. The same keys the Couchy API drops (`path`, `filename`, `token`,
...) are dropped from them at any depth.

## The guard on ComfyUI's own routes

The agent adds an aiohttp middleware to `PromptServer.instance.app`. ComfyUI builds the app
in `PromptServer.__init__` and loads custom nodes before it starts serving, so the middleware
can still be appended, and it runs before every handler (with and without the `/api` prefix).

- **`GET /view`** of a file whose `subfolder` lands under `couchy/<ns>/` needs a key for
  `<ns>`; `ro` is enough. The Couchy API proxies every image, so the app never notices.
- **`POST /upload/image`** and **`/upload/mask`** into `couchy/<ns>/` need an `rw` key for
  `<ns>`. A mask upload whose original is under `couchy/<ns>/` needs a key that can read it.
- **`POST /prompt`** is refused with 403 when any string in the graph names `couchy/<ns>/`
  for a namespace the caller holds no key for. A tenant can't load another tenant's upload
  by name through a stock node.
- **Everything outside `couchy/` is untouched.** Someone using ComfyUI's own UI on the same
  server keeps working, and `/view` for those paths costs one substring check.

Paths are compared after resolving `..`, doubled slashes, backslashes and symlinks, and
ignoring case, since macOS folders do.

**Once a tenant has paired**, couchyforms-helper's `/comfyform/files` routes answer 410 when
the helper runs in the same ComfyUI. They delete anything in `output`, `input` or `temp` for
anyone who can reach ComfyUI. `/comfyform/packs` is untouched.

## What this protects, and what it can't

| From whom | Protected? |
| --- | --- |
| Other people in the same Couchy workspace | Yes: the Couchy API checks ownership on every read and delete |
| Another Couchy tenant on the same ComfyUI | Yes, against accidents and casual reads: each tenant has its own namespace and key |
| Anyone on the network who can reach ComfyUI directly | Only if ComfyUI isn't reachable (below) |
| **A tenant with `runs.start` on a shared server who sets out to read another tenant's files** | **No.** Running a graph is running code on that server: a custom node like "Load image from path" reads anything on its disk. Tenants who must not trust each other need separate ComfyUI processes (below) or separate servers |
| **Whoever runs the GPU host or the API host** | **No.** They can read the disk. ComfyUI must read inputs and write outputs as plain files, so encryption at rest buys nothing against the host |

Also outside the guard: ComfyUI's assets system (off unless ComfyUI starts with
`--enable-assets`) serves files by hash, and the history (`/history`) holds every prompt's
file names until Couchy deletes it. Keep `--enable-assets` off on a paired server.

## Deployment rules

**Keep ComfyUI off the network.** Bind it to localhost (`--listen 127.0.0.1`), or put the
token proxy in front so only callers with the server token Couchy sends reach it. Couchy
warns on the server's card when ComfyUI answers without a token.

**Run ComfyUI as its own account, with private folders.** Nothing at the OS level separates
one person's files from another's: one ComfyUI process, as one OS user, reads and writes them
all. What the OS can do is keep other accounts on the same machine out.

- Make a service account for ComfyUI (for example `comfy`), with no shell and no sudo, not a
  person's login.
- Start ComfyUI with `umask 077`, so what it creates is `0700` folders and `0600` files.
- Fix the existing folders once:

  ```sh
  cd /path/to/ComfyUI
  sudo chown -R comfy: input output temp user models
  sudo chmod -R go-rwx input output temp user models
  ```

On start, the agent logs a warning for each of `input`, `output`, `temp`, `user` and
`models` that other accounts can reach (any group or world bit), and `GET /couchy/usage`
then reports `"permissions": "open"`. It creates `user/couchy-agent/` as `0700` and its
files as `0600` either way.

systemd (Linux), `/etc/systemd/system/comfyui.service`:

```ini
[Unit]
Description=ComfyUI
After=network-online.target

[Service]
User=comfy
Group=comfy
UMask=0077
WorkingDirectory=/srv/ComfyUI
ExecStart=/srv/ComfyUI/venv/bin/python main.py --listen 127.0.0.1 --port 8188
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

launchd (macOS), `/Library/LaunchDaemons/com.example.comfyui.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.example.comfyui</string>
  <key>UserName</key><string>comfy</string>
  <key>Umask</key><integer>63</integer><!-- 077 -->
  <key>WorkingDirectory</key><string>/opt/ComfyUI</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/ComfyUI/venv/bin/python</string><string>main.py</string>
    <string>--listen</string><string>127.0.0.1</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

Root and the service account itself can still read everything: that's the "whoever runs the
host" row above.

**For tenants who don't trust each other:** run one ComfyUI process per tenant, each as its
own service account with its own folders:

```sh
python main.py --port 8189 \
  --input-directory /srv/comfy-preprod/input \
  --output-directory /srv/comfy-preprod/output \
  --temp-directory /srv/comfy-preprod/temp
```

(or one container each). It isn't the default, since it doubles GPU memory on a shared card.

## Test

```sh
python3 -m unittest discover -s tests -t .
```

The tests are plain Python. The route tests need `aiohttp`, which ComfyUI already installs.
