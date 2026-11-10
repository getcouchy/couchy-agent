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
| `GET /couchy/usage` | `{namespace, folders: {input, output: {count, bytes, owners: {<owner>: {count, bytes}}}}, permissions: "ok" \| "open", scope_hook: "on" \| "off", nodes: <version> \| null}`. Never file names. `nodes` is the version of [Couchy's nodes](#couchys-file-nodes) once they're registered, so the API knows it can swap them in |
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

## Lockdown and self-pairing (cloud GPUs)

For a server Couchy created itself, such as a cloud GPU, two settings in the environment
change the agent. The cloud image (`cloud/`, see [its README](cloud/README.md)) sets both.

- **`COUCHY_AGENT_LOCKDOWN=1`.** Every route ComfyUI serves needs an agent key, not only
  `/view`, `/upload` and `/prompt`. That includes the websocket, `/object_info`,
  `/system_stats` and ComfyUI's own UI, which isn't supported on such a server. Two routes
  stay open, each with its own credential: `POST /couchy/pair` (a code or the token below) and
  `GET /couchy/models/blob/{sha256}` (a ticket). `GET /couchy/usage` reports
  `"lockdown": "on"`.
- **`COUCHY_AGENT_PAIRING_TOKEN`.** It's a one-time secret the API minted for this host.
  `POST /couchy/pair {code: <token>, label}` accepts it in place of a printed code, once, within
  30 minutes of the first agent start that saw it; a restart doesn't extend that or re-arm a
  spent token. **The key it answers has `models: write`**, because Couchy is who controls a host
  it created. A wrong, spent or expired token gets the same 403 as a wrong code. Only its hash
  is kept, in `user/couchy-agent/pairing-token.json`, and it's never logged.
- **`COUCHY_AGENT_ALLOWED_HOSTS`** (comma-separated) adds to the allowed hosts for pulls, so
  presigned pulls from the workspace's buckets work on a fresh host. `COUCHY_AGENT_INDEX_DIR`
  sets the index dir, ahead of `config --index-dir`.

## Models

The models routes say what's on this host's disks, what a `.safetensors` file's header
says, how much room each volume has and which models ran together, and they pull, delete and
hand copies to other agents when Couchy asks. The agent is mechanism only here too: it never
decides what to keep, load or evict, and it refuses what its host doesn't allow.

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

All but the blob need a key whose models grant is `read` (the first four) or `write` (the
rest), and all are also served under `/api/`. Every route that names a file takes `location`:
`host` (the default) or a share's name.

| Route | Does |
| --- | --- |
| `GET /couchy/models?[folder][&location][&after][&limit]` | A page (at most 1000) of model files, every folder and location unless narrowed: `{files: [{location, folder, path, size, mtime, sha256, verifiedAt}], next?}`. `sha256` and `verifiedAt` are null until the file is hashed. Pass `next` as `after` |
| `GET /couchy/models/header?folder&path[&location]` | A `.safetensors` file's header, read from its first bytes: `{metadata, tensors: [{name, dtype, shape}], tensorCount, truncated}`. `location` defaults to `host`. At most 2000 tensors are listed. 422 for any other format and for a header over 1 MB; 404 for a missing file or a symlink |
| `GET /couchy/models/space` | Each volume that holds a models folder: `{volumes: [{location, folders, freeBytes, totalBytes}]}` |
| `GET /couchy/models/history?[since]` | Which hashed models ran together in ComfyUI's history: `{runs: [{at, sha256: [...]}]}`, oldest first, `at` in seconds, after `since` when given. No prompts, prompt ids, images or other inputs. Empty in store mode |
| `GET /couchy/models/pulls/{id}` | `{id, state: queued \| running \| verifying \| done \| failed, bytesDone, bytesTotal, error?}`. 404 for a pull this agent doesn't have, as after a restart |
| `POST /couchy/models/pull {sha256, location, folder, path, size, source}` | `write`. Starts a pull and answers `{id}` at once (the same id for a pull of the same file to the same place that's still running). `source` is `{url, headers?}`, `{peer, ticket}` (`peer` being another agent's base URL) or `{presigned}` (a bucket object's presigned URL, minted by the API for this pull) |
| `POST /couchy/models/pulls/{id}/cancel` | `write`. Stops it and removes the partial file; answers its state |
| `POST /couchy/models/delete {sha256, location, folder, path}` | `write`. Deletes the file only when its indexed hash is `sha256`. Answers `{state: deleted \| missing \| mismatch \| refused}`, with a `reason` for refused (`not_hashed` yet, a `symlink` or a bad `path`) |
| `POST /couchy/models/tickets {sha256, location, ttl?}` | `write`. A one-time ticket for the file with that hash there, for another agent's pull: `{ticket, expiresAt}`. `ttl` is seconds, 300 by default and at most 3600. 404 when no hashed file there has that hash |
| `GET /couchy/models/blob/{sha256}` | No key: a ticket, in `X-Couchy-Agent-Ticket` or `?ticket`. Streams the file, with `Range`. A ticket is spent the first time it's shown, and is for one file and location; 403 otherwise |

`error` on a failed pull is one of `exists` (a different file is at the target), `hash_mismatch`,
`size_mismatch`, `pickle_refused`, `format_refused`, `host_not_allowed`, `no_room`,
`ticket_refused`, `not_found`, `source_refused`, `source_failed`, `too_many_redirects`,
`network`, `io`, `location_missing`, `bad_source` or `cancelled`.

A listing is the inventory as of the last scan: the host is scanned every minute and shares
every five, so a NAS isn't walked on every page.

### Hashing

Hashing a 23 GB checkpoint takes minutes, so it never runs on a request. A background thread
hashes new and changed files one at a time, at low priority (a niced thread on Linux), across
`host` and every share, and keeps `{location, folder, path, size, mtime} → {sha256,
verifiedAt}` in `user/couchy-agent/models-index.json` (`0600`). After a restart only files
whose size or mtime changed are hashed again, and an index that's missing or can't be read is
rebuilt in the background.

**`--index-dir`** keeps the index in another folder, written atomically and `0600`. Use it when
the models folder outlives the host, such as a RunPod network volume mounted as ComfyUI's models
folder: with the index on the volume (`config --index-dir /workspace/models/.couchy-index`, read
when ComfyUI starts), a new pod rehashes only what changed instead of 400 GB. A folder inside the
models root is fine; it isn't a models folder, so it's never listed. Store mode takes
`store --index-dir` too.

Each file is **re-verified weekly** by hashing it again. A match sets `verifiedAt`. A mismatch
(the bytes changed under the same size and mtime) clears `sha256` and `verifiedAt`, logs
`model verify mismatch` as a warning, and leaves the file unhashed until it changes. A share
that turns up empty or missing (a mount that failed) keeps its hashes until it's back.

### Pulls

- **Into a partial file, then renamed.** A pull streams into `.couchy-partial/<id>` under its
  location's root (under the target folder instead when that's on another volume), hashing as
  it goes. It checks the size and SHA-256, then renames the file into place without replacing
  anything (`renameat2` with `RENAME_NOREPLACE`, else a hard link then unlink). A different file
  at the target fails the pull with `exists`; the same file, already hashed, makes it `done` at
  once. `.couchy-partial` is never listed, hashed or served.
- **Resumes.** A pull's id comes from its hash and target, so when the API starts it again after
  a failure or a restart (the pull answers 404 then: pulls are kept in memory) it picks up the
  partial file with `Range`. A source that ignores `Range` starts over. A network error keeps the
  partial file; a mismatch or a cancel removes it.
- **A share is a target** like `host`, so a GPU host that mounts nas-cold can copy to it.
- **Safe formats only:** `.safetensors`, `.gguf` and `.sft`. Pickle formats (`.ckpt`, `.pt`,
  `.pth`, `.bin`) run code when ComfyUI loads them, and fail with `pickle_refused` unless the host
  allows them. Anything else fails with `format_refused`.
- **Known hosts only:** `civitai.com`, `huggingface.co`, `hf.co` and their subdomains, and
  Civitai's download bucket, over HTTPS; plus a peer agent with a ticket. Anything else, a
  redirect included, needs the host's allowed hosts (HTTP or HTTPS) and otherwise fails with
  `host_not_allowed`. A GPU host is never a fetch-anything proxy.
- **A presigned URL** (a bucket object, valid minutes) is fetched only when its host is in the
  allowed hosts (being Civitai or Hugging Face isn't enough), and it never follows a redirect. Add the bucket's
  endpoint, such as `r2.cloudflarestorage.com`, to fetch from it.
- **Credentials are for that pull only.** A Civitai token in the URL, a Hugging Face
  `Authorization` header, a ticket or a presigned URL is kept in memory while the pull runs. It's never written
  to disk or logged, and headers aren't sent on to another host on a redirect.
- **Two at a time:** at most two pulls run and the rest queue.
- **Room:** a pull that would leave less than the reserve free on its volume (20 GB by default)
  fails with `no_room`. The agent never evicts.

The host's own rules, set as the account ComfyUI runs as:

```sh
sudo -u comfy python -m couchy_agent config --allowed-host models.example.com   # repeat; replaces the list
sudo -u comfy python -m couchy_agent config --clear-allowed-hosts
sudo -u comfy python -m couchy_agent config --allow-pickle                      # or --no-allow-pickle
sudo -u comfy python -m couchy_agent config --reserve-gb 50
sudo -u comfy python -m couchy_agent config --index-dir /workspace/models/.couchy-index   # or --clear-index-dir
```

An allowed host covers its subdomains. These sit in `config.json` beside the shares, and a
running agent reads them on its next pull.

### Store mode

For a host that keeps model copies but runs no ComfyUI:

```sh
pip install aiohttp            # Python 3.10+ and aiohttp are all it needs
python -m couchy_agent store --root /volume1/models --listen 0.0.0.0:8190 [--share nas-cold=/volume2/models]
```

It serves the same models routes, pairing and keys, and hashes in the background, with
`--root` (laid out like ComfyUI's `models/`) as `host`. There's no `/view`, guard, namespaced
files or history. Its state lives in `~/.local/state/couchy-agent` (or `--user-directory`), and
`pair`, `keys` and `config` use the same folder when no ComfyUI is around. `--listen` defaults to
`127.0.0.1:8190`; anywhere beyond localhost, **put it behind TLS** (a reverse proxy), since its
key and tickets travel on every call.

**Which to use:** when the store is a share that GPU hosts already mount (the lab's NAS, as
CIFS on spark-1 and comfy-2), use `--share` on those hosts and run nothing on the NAS. Store mode
is for a machine that holds copies no GPU host mounts.

### Logging

The models lines are JSON, one object per line on ComfyUI's console, each with
`service: "couchy-agent"`, `version` and `server` (the host name). They name a file by its
location, folder and the first 12 hex characters of its SHA-256 (`sha`), with `bytes` and
`ms`, and a pull by its id. Never a path, a URL, a header or a ticket. The same keys the Couchy API drops (`path`, `filename`, `token`,
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
