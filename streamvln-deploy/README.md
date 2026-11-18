# StreamVLN deployment (patched policy server)

The StreamVLN real-world policy server as deployed in this workspace, with the
minimal patches the dimos `stream_vln` skill integration needs
(`dimos/dimos/navigation/experimental/stream_vln/skill.py` is its HTTP client).
The patched server itself lives in this directory so the deployment is
reproducible from this repository alone — nothing is fixed only in `/tmp`.

## Upstream

- Repo: https://github.com/InternRobotics/StreamVLN (ICRA 2026,
  "StreamVLN: Streaming Vision-and-Language Navigation via SlowFast Context
  Modeling")
- Server file upstream: `streamvln/http_realworld_server.py`
- The upstream tree (code only: the packages `streamvln/` and `model/`, which
  the server imports) must exist somewhere on the machine; the scripts default
  to `/tmp/StreamVLN` and the location can be overridden with the
  `STREAMVLN_CODE_ROOT` environment variable. Re-clone upstream to that path
  (or any path + the env var) to reproduce the deployment.
- `http_realworld_server.patch` in this directory is the exact diff of the
  patched copy vs the pristine upstream file (`patch -p1` from inside an
  upstream checkout re-creates this copy), for reviewing what changed.

## Patches and why

1. **Instruction passthrough.** The upstream server hardcodes the navigation
   instruction ("Walk forward and immediately stop when you exit the room."),
   and the upstream client never sends one (`realworld/go2_vln_client.py`
   `eval_vln` posts only `{"reset": ...}`) — so upstream can only follow its
   one baked-in instruction. The patched server reads `instruction` from the
   client's `json` form field and injects it into the policy prompt when the
   evaluator builds its conversation (the first inference after a `reset`);
   requests without the key keep the upstream default, so the official client
   still works unmodified.
2. **`STREAMVLN_CODE_ROOT`.** The code root (the checkout containing
   `streamvln/` and `model/`) is an env var instead of the script's parent
   directory, because this copy lives outside the upstream checkout.
3. **Instruction logging.** The active instruction is printed per request
   (deployment log evidence that a task's instruction actually reached the
   server).

Not patched: the BGR→RGB channel flip (`np.asarray(image)[..., ::-1]`).
Sending BGR-ordered JPEG is the upstream client contract, and the dimos skill
matches it (`active_vln.skill._image_to_bgr_jpeg`); changing the flip would
break the official client.

## Running

Everything below is done by `~/workspace/dimos_ws/run_go2_streamvln.sh`; the
manual equivalent:

```bash
cd /tmp/StreamVLN/streamvln   # CWD: the server writes runs*/ annotated frames here
STREAMVLN_CODE_ROOT=/tmp/StreamVLN CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 \
  ~/workspace/dimos_ws/streamvln-serve/bin/python \
  ~/workspace/dimos_ws/streamvln-deploy/http_realworld_server.py \
  --model_path ~/workspace/dimos_ws/streamvln-model \
  --device cuda:0
# -> http://0.0.0.0:5801/eval_vln  (port hardcoded by the server)
```

- **Python env**: `~/workspace/dimos_ws/streamvln-serve` (torch 2.7.1+cu126 —
  do not let deepspeed upgrade it, it breaks the flash-attn ABI; transformers
  4.40.0; the official flash-attn prebuilt wheel; see the run script header).
- **Checkpoint**: `~/workspace/dimos_ws/streamvln-model` (symlink; HF
  `mengwei0427/StreamVLN_Video_qwen_1_5_r2r_rxr_envdrop_scalevln_v1_3` — the
  checkpoint ships without tokenizer files; the Qwen1.5-1.8B ones are copied
  in). GPU 0 (GPU 1 holds the ActiveVLN vllm).

## Wire protocol (`POST /eval_vln`)

Multipart form: `image` = JPEG file (BGR channel order — upstream client
contract, see above), `json` = `{"reset": bool, "instruction": str}`.
Response: `{"action": [ints]}` with ids 0 stop / 1 forward 25cm / 2 left 15° /
3 right 15°, up to 4 actions per answer (one SlowFast inference per 4 internal
steps).

- `reset: true` wipes the policy's memory tokens / KV cache and starts a new
  episode; the instruction is only injected while the conversation is being
  built (the first inference after a reset), so a per-task `reset + instruction`
  post starts each task with the task's instruction and no context pollution.
- The server latches "terminated" once an answer contains id 0; further posts
  return `[0]` until the next `reset`.
