# couchy-agent

A ComfyUI custom node pack for [Couchy](https://github.com/getcouchy/couchy). It keeps each Couchy tenant's files on the server in a folder of its own, and each
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
only each key's hash, bound to its namespace and a mode, in `user/couchy-agent/keys.json`.

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

## Routes

All routes are also served under `/api/`, like ComfyUI's own. `folder` is `input` or
`output`; `subfolder` is relative to that folder, as ComfyUI's `/view` takes it.

| Route | Does |
| --- | --- |
| `POST /couchy/pair {code, label}` | No key. Spends a code; answers `{namespace, key, keyId, mode: "rw"}`. 403 for a wrong, used or expired code, 429 when locked out |
| `GET /couchy/usage` | `{namespace, folders: {input, output: {count, bytes, owners: {<owner>: {count, bytes}}}}, permissions: "ok" \| "open", scope_hook: "on" \| "off", nodes: <version> \| null}`. Never file names. `nodes` is the version of [Couchy's nodes](#couchys-file-nodes) once they're registered, so the API knows it can swap them in |
| `GET /couchy/files?folder&owner[&after][&limit]` | A page (at most 1000) of one owner's files: `{files: [{subfolder, filename, size, mtime}], total: {count, bytes}, next?}`. Pass `next` as `after` |
| `POST /couchy/files/delete {folder, files: [{subfolder, filename}]}` | Answers `{deleted, missing, refused}`, each a list |
| `POST /couchy/files/delete {folder, owner}` | Deletes all of one owner's files; answers `{deleted, bytes}` |
| `POST /couchy/files/delete {folder, olderThan}` | Deletes the namespace's files last changed more than `olderThan` seconds ago; answers `{deleted, bytes}` |
| `POST /couchy/files/adopt {from: {folder, subfolder, filename}, owner, runId}` | Moves a file from `output` or `temp`, outside `couchy/`, into `output/couchy/<ns>/<owner>/<runId>/`. Answers `{state: "adopted", to: {folder, subfolder, filename}}`, `{state: "missing"}` (gone, as on a second adopt of the same file) or `{state: "refused"}` (another namespace, outside the folder, a symlink). A taken name gets `_1`, `_2` ... |
| `POST /couchy/files/move-owner {from, to}` | Re-homes one owner's files in `input` and `output`; answers `{input: {moved}, output: {moved}}` |
| `GET /couchy/keys` | The namespace's key ids, modes and labels |
| `POST /couchy/keys {mode: "ro" \| "rw", label}` | Mints another key for the caller's namespace (`rw` caller only). Answers `{id, key, namespace, mode}` once; the key isn't kept |
| `POST /couchy/keys/revoke {id}` | Revokes another key of the namespace (`rw` caller only; not itself). Answers `{revoked}` |

The path rules are those of couchyforms-helper's `files.py`: every path is resolved,
symlinks included, and must stay inside its folder; symlinks are never listed; folders are
never deleted. On top of that, inside `couchy/` only the key's namespace is reachable.
Owners and run ids are letters, digits, `_` and `-`.

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

`/prompt` also binds the run's scope; see [Run scopes](#run-scopes).

**Once a tenant has paired**, couchyforms-helper's `/comfyform/files` routes answer 410 when
the helper runs in the same ComfyUI. They delete anything in `output`, `input` or `temp` for
anyone who can reach ComfyUI. `/comfyform/packs` is untouched.

## Run scopes

The API sends each run's scope on `POST /prompt`, with its `rw` key:

```json
{"prompt": {...}, "extra_data": {"couchy": {"runId": "r_1", "owner": "u_1", "inputs": ["up_1", "up_2"]}}}
```

`inputs` are upload ids the API's `canRead` allowed for this run (the person's own and Lab's
shared ones); `"metadata": false` also leaves the prompt and workflow out of saved PNGs. The
guard checks the key is `rw` (403 otherwise, 400 for a malformed scope), binds the scope to the
prompt id (setting `prompt_id` when the API sent none) and **removes `extra_data.couchy` before
ComfyUI reads the body**, so it never reaches the history. A `prompt_id` that already has a
scope is refused with 409, from anyone, so no second prompt can run under it.

Scopes live in memory: they go when the queue finishes the prompt (success, error or
interrupt), when it's deleted from the queue or the queue is wiped, and when ComfyUI refuses
the prompt. A restart loses the queue anyway.

The agent doesn't decide who may read what. It refuses only what the API didn't list.

**Until the API sends scopes** (couchy #981), a prompt with an `rw` key and no
`extra_data.couchy` gets a namespace scope: it reaches all of its key's namespace, which the key
already can through `/view`, and nothing else inside `couchy/`. A prompt with no key, or an `ro`
key, has no scope.

## Couchy's file nodes

On a paired server the agent registers five V3 nodes (`comfy_api.latest`) under the category
`couchy`. A server paired after start-up gets them at pairing. They take an upload id, not a
path, and the work is the stock node's on the resolved file, so outputs, EXIF and alpha handling,
numbering and PNG metadata are ComfyUI's own.

| Class | Display name | Replaces | Inputs | Does |
| --- | --- | --- | --- | --- |
| `CouchyLoadImage` | Couchy Load Image | `LoadImage` | `upload` | Opens `input/couchy/<ns>/<owner>/<upload>.*` when the upload is in the run's scope. IMAGE, MASK |
| `CouchyLoadMask` | Couchy Load Mask | `LoadImageMask` | `upload`, `channel` | As above. MASK |
| `CouchyLoadVideo` | Couchy Load Video | `LoadVideo` (core) | `upload` | As above. VIDEO |
| `CouchySaveImage` | Couchy Save Image | `SaveImage` | `images` | Writes `output/couchy/<ns>/<owner>/<runId>/ComfyUI_00001_.png` ..., whatever prefix the graph had |
| `CouchyPreviewImage` | Couchy Preview Image | `PreviewImage` | `images` | Writes to `temp/couchy/<ns>/<owner>/<runId>/` |

Without a run scope (someone in ComfyUI's own UI) each fails with "This node only runs in runs
Couchy starts."; an upload that isn't in the scope fails with "This file isn't part of this
run.", naming the node. Saves are never answered from another run's cache. Video saves keep the
API's prefix rewrite; there's no Couchy video save node.

## The scope hook

The agent wraps `folder_paths.get_annotated_filepath`, `exists_annotated_filepath` and
`get_save_image_path`, the calls stock nodes and most packs use to resolve files, once at load:

- **Inside `couchy/`**, a load is allowed only under `couchy/<ns>/<owner>/` for the run's owner,
  or for an upload in its scope (`input/couchy/<ns>/<anyone>/<upload>.*`). A save must be in the
  run's folder in `output/` or `temp/`, and is checked before ComfyUI makes the folder.
  Anything else raises "This file isn't part of this run." and the run fails with that node
  named.
- **Outside `couchy/`**, paths are untouched, so model loaders and ComfyUI's own UI keep working.
- **A prompt with no scope** gets nothing inside `couchy/` through these calls.

Paths are resolved, symlinks included, before they're judged, so a `[output]` suffix, `..` or a
path built at run time lands where it really points. Nodes made by expansion run under their
prompt's id and are held the same way. A pack that opens files itself, or that imported these
functions by name before the agent loaded, isn't held; that's the API's file-access check
(couchy #982).

The hook is pinned to the signatures of ComfyUI v0.42.5 (483dae4, the runtime manifest's
commit). When they differ, the agent doesn't install it, logs why, and `GET /couchy/usage`
reports `"scope_hook": "off"`.

Logs name the prompt id, the run id and the refused node's class, never file paths.

## What this protects, and what it can't

| From whom | Protected? |
| --- | --- |
| Other people in the same Couchy workspace | Yes: the Couchy API checks ownership on every read and delete |
| Another Couchy tenant on the same ComfyUI | Yes, against accidents and casual reads: each tenant has its own namespace and key |
| Anyone on the network who can reach ComfyUI directly | Only if ComfyUI isn't reachable (below) |
| **A tenant with `runs.start` on a shared server who sets out to read another tenant's files** | **Partly.** Stock nodes and packs that resolve files through `folder_paths` are held to the run's scope by the [scope hook](#the-scope-hook). But running a graph is running code on that server: a custom node like "Load image from path" that opens files itself reads anything on its disk. Tenants who must not trust each other need separate ComfyUI processes (below) or separate servers |
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

`tests/test_comfyui.py` runs the nodes, the hook and the queue wrap inside a real ComfyUI (its
`folder_paths`, stock nodes, `validate_prompt`, `PromptExecutor` and `PromptQueue`, on the
CPU). It's skipped unless `COMFYUI_PATH` names a checkout at the runtime manifest's commit, with
ComfyUI's requirements installed:

```sh
git clone https://github.com/comfyanonymous/ComfyUI ~/ComfyUI && git -C ~/ComfyUI checkout 483dae4
pip install -r ~/ComfyUI/requirements.txt
COMFYUI_PATH=~/ComfyUI python3 -m unittest discover -s tests -t .
```
