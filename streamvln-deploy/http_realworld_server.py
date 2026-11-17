# StreamVLN real-world policy server — patched deployment copy.
#
# Upstream: https://github.com/InternRobotics/StreamVLN (ICRA 2026), file
# `streamvln/http_realworld_server.py`. The upstream tree this was patched
# against is deployed at /tmp/StreamVLN (see README.md for reproduction).
#
# Patch vs upstream (see http_realworld_server.patch for the exact diff;
# everything not listed is upstream's code unchanged):
#   1. The navigation instruction is read from the client's request (the
#      `json` form field, "instruction" key) instead of a hardcoded string.
#      Upstream's own real-world client never sends one (realworld/
#      go2_vln_client.py eval_vln posts only {"reset": ...}), so upstream
#      can only ever follow its one baked-in instruction; dimos's
#      stream_vln skill sends the user's instruction per task. Requests
#      without an "instruction" key keep the upstream default (backward
#      compatible).
#   2. The StreamVLN code root (the checkout containing the packages
#      `streamvln/` and `model/`) is taken from STREAMVLN_CODE_ROOT
#      (default /tmp/StreamVLN) instead of this script's parent directory —
#      this copy does not live inside the upstream checkout.
#   3. The active instruction is printed per request (deployment log
#      evidence that the task's instruction reached the server).
#
# Not patched, on purpose: the BGR->RGB channel flip below
# (np.asarray(image)[..., ::-1]). Clients must send BGR-ordered JPEG, like
# the official client does (cv_bridge 'bgr8' wrapped by PIL); dimos's
# stream_vln skill does the same (active_vln.skill._image_to_bgr_jpeg).

import argparse
import numpy as np
import json
import time
import torch
import sys
import os
import transformers

# Patch 2: the StreamVLN code root (the checkout containing the packages
# `streamvln/` and `model/`) is taken from STREAMVLN_CODE_ROOT (default
# /tmp/StreamVLN) instead of this script's parent directory — this copy does
# not live inside the upstream checkout. Both entries the original had are
# added: the root (for the `streamvln` package) and its streamvln/ subdir
# (the original's implicit sys.path[0] as the script's own directory, where
# the `model` package lives).

_streamvln_code_root = os.environ.get("STREAMVLN_CODE_ROOT", "/tmp/StreamVLN")
sys.path.insert(0, _streamvln_code_root)
sys.path.insert(0, os.path.join(_streamvln_code_root, "streamvln"))

from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
from datetime import datetime
from streamvln.streamvln_agent import VLNEvaluator
from model.stream_video_vln import StreamVLNForCausalLM

app = Flask(__name__)
DEFAULT_INSTRUCTION = "Walk forward and immediately stop when you exit the room."
action_seq = np.zeros(4)
idx = 0
terminate = False
total_generate_time = 0.0
start_time = time.time()
output_dir = ''
def annotate_image(idx, image, start_time, total_generate_time, llm_output, output_dir):
    image = Image.fromarray(image)#.save(f'rgb_{idx}.png')
    draw = ImageDraw.Draw(image)
    font_size = 20
    font = ImageFont.truetype("DejaVuSansMono.ttf", font_size)
    text_content = []
    text_content.append(f"Frame    Id  : {idx}")
    text_content.append(f"Running  time: {time.time() - start_time:.2f} s")
    text_content.append(f"Generate time: {total_generate_time:.2f} s")
    text_content.append(f"Actions      : {llm_output}" )
    max_width = 0
    total_height = 0
    for line in text_content:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = 26
        max_width = max(max_width, text_width)
        total_height += text_height

    padding = 10
    box_x, box_y = 10, 10
    box_width = max_width + 2 * padding
    box_height = total_height + 2 * padding

    draw.rectangle([box_x, box_y, box_x + box_width, box_y + box_height], fill='black')

    text_color = 'white'
    y_position = box_y + padding

    for line in text_content:
        draw.text((box_x + padding, y_position), line, fill=text_color, font=font)
        bbox = draw.textbbox((0, 0), line, font=font)
        text_height = 26
        y_position += text_height

    image.save(f'{output_dir}/rgb_{idx}_annotated.png')

@app.route("/eval_vln",methods=['POST'])
def eval_vln():
    global action_seq, idx, terminate, total_generate_time, output_dir, start_time

    image_file = request.files['image']
    json_data = request.form['json']
    data = json.loads(json_data)

    image = Image.open(image_file.stream)
    image = image.convert('RGB')
    image = np.asarray(image)[...,::-1]

    camera_pose = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
    # Patch 1: the instruction comes from the request when the client sends
    # one (dimos's stream_vln skill does, on every post); the upstream
    # default otherwise. The trailing period keeps the evaluator's splice
    # clean: it replaces the literal "<instruction>." in its prompt.
    instruction = str(data.get('instruction') or DEFAULT_INSTRUCTION).strip()
    if instruction and not instruction.endswith('.'):
        instruction += '.'
    print(f"instruction: {instruction!r}", flush=True)


    policy_init = data['reset']
    if policy_init:
        start_time = time.time()
        total_generate_time = 0.0
        terminate = False
        idx = 0
        output_dir = 'runs' + datetime.now().strftime('%m-%d-%H%M')
        os.makedirs(output_dir, exist_ok=True)
        print("init reset model!!!")
        evaluator.reset_memory()

    idx += 1

    if terminate:
        print("!!!!!!!!!!!!!!!!!task finish!!!!!!!!!!!!!!!!!!!!!")
        return jsonify({'action': [0]})

    for i in range(4):
        t1 = time.time()
        depth = np.zeros((image.shape[0], image.shape[1], 1))
        return_action, generate_time, return_llm_output = evaluator.step(0,
                                        image,
                                        #depth,
                                        #camera_pose,
                                        instruction,
                                        run_model=(evaluator.step_id % 4 == 0))
        llm_output = return_llm_output if return_llm_output is not None else llm_output
        print(f"one evalute cost {time.time() - t1}")
        # total_generate_time += generate_time

        if generate_time > 0:
            total_generate_time = generate_time
        action_seq = action_seq if return_action is None else return_action
        if 0 in action_seq:
            terminate = True
        evaluator.step_id += 1

    str_action = [str(i) for i in action_seq]
    str_action = ''.join(str_action)
    str_action = str_action.replace('1', '↑')  # 前箭头
    str_action = str_action.replace('2', '←')  # 左箭头
    str_action = str_action.replace('3', '→')  # 右箭头
    str_action = str_action.replace('0', 'STOP')  # 停止
    if idx > 1 and total_generate_time > 0.5:
        total_generate_time -= 0.3

    annotate_image(idx, image, start_time, total_generate_time, str_action, output_dir)

    if len(action_seq) == 0:
        print("!!!!!!!!!!!!!!!!!task finish!!!!!!!!!!!!!!!!!!!!!")
        return jsonify({'action': [0]})

    return jsonify({'action': action_seq})

if __name__ == '__main__':
    global local_rank
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_path", type=str, default="/home/pjlab/yq_ws/StreamVLN/checkpoints/StreamVLN_Video_qwen_1_5_r2r_rxr_envdrop_scalevln_real_world")
    parser.add_argument("--num_future_steps", type=int, default=4)
    parser.add_argument("--num_frames", type=int, default=32)
    parser.add_argument("--num_history", type=int, default=8)
    parser.add_argument("--model_max_length", type=int, default=4096,
                        help= "Maximum sequence length. Sequences will be right padded (and possibly truncated).")
    parser.add_argument('--device', default='cuda:0',
                        help='device to use for testing')

    args = parser.parse_args()

    tokenizer = transformers.AutoTokenizer.from_pretrained(args.model_path,
                                                        model_max_length=args.model_max_length,
                                                        padding_side="right")

    config = transformers.AutoConfig.from_pretrained(args.model_path)
    model = StreamVLNForCausalLM.from_pretrained(
                args.model_path,
                attn_implementation="flash_attention_2",
                torch_dtype=torch.bfloat16,
                config=config,
                low_cpu_mem_usage=False,
                )
    model.model.num_history = args.num_history
    model.reset(1)
    model.requires_grad_(False)
    model.to(args.device)
    model.eval()


    vln_sensor_config = {
        "rgb_height" : 1.25,
        "camera_intrinsic" : np.array([[192.        ,   0.        , 191.42857143,   0.        ],
            [  0.        , 192.        , 191.42857143,   0.        ],
            [  0.        ,   0.        ,   1.        ,   0.        ],
            [  0.        ,   0.        ,   0.        ,   1.        ]]),
    }

    evaluator = VLNEvaluator(
        vln_sensor_config,
        model=model,
        tokenizer=tokenizer,
        args=args,
    )


    evaluator.step(0, np.zeros((480, 640, 3), dtype=np.uint8), "move forward 25 cm", run_model=True)
    app.run(host='0.0.0.0', port=
            5801)
