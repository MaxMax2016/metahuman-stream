# server.py
from flask import Flask, request, jsonify
from flask_sockets import Sockets
import base64
import time
import json
import gevent
from gevent import pywsgi
from geventwebsocket.handler import WebSocketHandler
import os
import re
import numpy as np
from threading import Thread
import multiprocessing

import argparse
from nerf_triplane.provider import NeRFDataset_Test
from nerf_triplane.utils import *
from nerf_triplane.network import NeRFNetwork
from nerfreal import NeRFReal

import shutil
import asyncio
import edge_tts
from typing import Iterator

import requests

app = Flask(__name__)
sockets = Sockets(app)
global nerfreal
global tts_type
global gspeaker


async def main(voicename: str, text: str, render):
    communicate = edge_tts.Communicate(text, voicename)

    #with open(OUTPUT_FILE, "wb") as file:
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            render.push_audio(chunk["data"])
            #file.write(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            pass                

def get_speaker(ref_audio,server_url):
    files = {"wav_file": ("reference.wav", open(ref_audio, "rb"))}
    response = requests.post(f"{server_url}/clone_speaker", files=files)
    return response.json()

def xtts(text, speaker, language, server_url, stream_chunk_size) -> Iterator[bytes]:
    start = time.perf_counter()
    speaker["text"] = text
    speaker["language"] = language
    speaker["stream_chunk_size"] = stream_chunk_size  # you can reduce it to get faster response, but degrade quality
    res = requests.post(
        f"{server_url}/tts_stream",
        json=speaker,
        stream=True,
    )
    end = time.perf_counter()
    print(f"xtts Time to make POST: {end-start}s")

    if res.status_code != 200:
        print("Error:", res.text)
        return

    first = True
    for chunk in res.iter_content(chunk_size=960):
        if first:
            end = time.perf_counter()
            print(f"xtts Time to first chunk: {end-start}s")
            first = False
        if chunk:
            yield chunk

    print("xtts response.elapsed:", res.elapsed)

def stream_xtts(audio_stream,render):
    for chunk in audio_stream:
        if chunk is not None:
            render.push_audio(chunk)

def txt_to_audio(text_):
    if tts_type == "edgetts":
        voicename = "zh-CN-YunxiaNeural"
        text = text_
        t = time.time()
        asyncio.get_event_loop().run_until_complete(main(voicename,text,nerfreal))
        print(f'-------edge tts time:{time.time()-t:.4f}s')
    else: #xtts
        stream_xtts(
            xtts(
                text_,
                gspeaker,
                "zh-cn", #en args.language,
                "http://localhost:9000", #args.server_url,
                "20" #args.stream_chunk_size
            ),
            nerfreal
        )

    
@sockets.route('/humanecho')
def echo_socket(ws):
    # 获取WebSocket对象
    #ws = request.environ.get('wsgi.websocket')
    # 如果没有获取到，返回错误信息
    if not ws:
        print('未建立连接！')
        return 'Please use WebSocket'
    # 否则，循环接收和发送消息
    else:
        print('建立连接！')
        while True:
            message = ws.receive()           
            
            if len(message)==0:
                return '输入信息为空'
            else:                                
                txt_to_audio(message)


def llm_response(message):
    from llm.LLM import LLM
    # llm = LLM().init_model('Gemini', model_path= 'gemini-pro',api_key='Your API Key', proxy_url=None)
    llm = LLM().init_model('ChatGPT', model_path= 'gpt-3.5-turbo',api_key='Your API Key')
    response = llm.chat(message)
    print(response)
    return response

@sockets.route('/humanchat')
def chat_socket(ws):
    # 获取WebSocket对象
    #ws = request.environ.get('wsgi.websocket')
    # 如果没有获取到，返回错误信息
    if not ws:
        print('未建立连接！')
        return 'Please use WebSocket'
    # 否则，循环接收和发送消息
    else:
        print('建立连接！')
        while True:
            message = ws.receive()           
            
            if len(message)==0:
                return '输入信息为空'
            else:
                res=llm_response(message)                           
                txt_to_audio(res)                        

def render():
    nerfreal.render()                  
               

if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--pose', type=str, default="data/data_kf.json", help="transforms.json, pose source")
    parser.add_argument('--au', type=str, default="data/au.csv", help="eye blink area")

    parser.add_argument('-O', action='store_true', help="equals --fp16 --cuda_ray --exp_eye")

    parser.add_argument('--data_range', type=int, nargs='*', default=[0, -1], help="data range to use")
    parser.add_argument('--workspace', type=str, default='data/video')
    parser.add_argument('--seed', type=int, default=0)

    ### training options
    parser.add_argument('--ckpt', type=str, default='data/pretrained/ngp_kf.pth')
   
    parser.add_argument('--num_rays', type=int, default=4096 * 16, help="num rays sampled per image for each training step")
    parser.add_argument('--cuda_ray', action='store_true', help="use CUDA raymarching instead of pytorch")
    parser.add_argument('--max_steps', type=int, default=16, help="max num steps sampled per ray (only valid when using --cuda_ray)")
    parser.add_argument('--num_steps', type=int, default=16, help="num steps sampled per ray (only valid when NOT using --cuda_ray)")
    parser.add_argument('--upsample_steps', type=int, default=0, help="num steps up-sampled per ray (only valid when NOT using --cuda_ray)")
    parser.add_argument('--update_extra_interval', type=int, default=16, help="iter interval to update extra status (only valid when using --cuda_ray)")
    parser.add_argument('--max_ray_batch', type=int, default=4096, help="batch size of rays at inference to avoid OOM (only valid when NOT using --cuda_ray)")

    ### loss set
    parser.add_argument('--warmup_step', type=int, default=10000, help="warm up steps")
    parser.add_argument('--amb_aud_loss', type=int, default=1, help="use ambient aud loss")
    parser.add_argument('--amb_eye_loss', type=int, default=1, help="use ambient eye loss")
    parser.add_argument('--unc_loss', type=int, default=1, help="use uncertainty loss")
    parser.add_argument('--lambda_amb', type=float, default=1e-4, help="lambda for ambient loss")

    ### network backbone options
    parser.add_argument('--fp16', action='store_true', help="use amp mixed precision training")
    
    parser.add_argument('--bg_img', type=str, default='white', help="background image")
    parser.add_argument('--fbg', action='store_true', help="frame-wise bg")
    parser.add_argument('--exp_eye', action='store_true', help="explicitly control the eyes")
    parser.add_argument('--fix_eye', type=float, default=-1, help="fixed eye area, negative to disable, set to 0-0.3 for a reasonable eye")
    parser.add_argument('--smooth_eye', action='store_true', help="smooth the eye area sequence")

    parser.add_argument('--torso_shrink', type=float, default=0.8, help="shrink bg coords to allow more flexibility in deform")

    ### dataset options
    parser.add_argument('--color_space', type=str, default='srgb', help="Color space, supports (linear, srgb)")
    parser.add_argument('--preload', type=int, default=0, help="0 means load data from disk on-the-fly, 1 means preload to CPU, 2 means GPU.")
    # (the default value is for the fox dataset)
    parser.add_argument('--bound', type=float, default=1, help="assume the scene is bounded in box[-bound, bound]^3, if > 1, will invoke adaptive ray marching.")
    parser.add_argument('--scale', type=float, default=4, help="scale camera location into box[-bound, bound]^3")
    parser.add_argument('--offset', type=float, nargs='*', default=[0, 0, 0], help="offset of camera location")
    parser.add_argument('--dt_gamma', type=float, default=1/256, help="dt_gamma (>=0) for adaptive ray marching. set to 0 to disable, >0 to accelerate rendering (but usually with worse quality)")
    parser.add_argument('--min_near', type=float, default=0.05, help="minimum near distance for camera")
    parser.add_argument('--density_thresh', type=float, default=10, help="threshold for density grid to be occupied (sigma)")
    parser.add_argument('--density_thresh_torso', type=float, default=0.01, help="threshold for density grid to be occupied (alpha)")
    parser.add_argument('--patch_size', type=int, default=1, help="[experimental] render patches in training, so as to apply LPIPS loss. 1 means disabled, use [64, 32, 16] to enable")

    parser.add_argument('--init_lips', action='store_true', help="init lips region")
    parser.add_argument('--finetune_lips', action='store_true', help="use LPIPS and landmarks to fine tune lips region")
    parser.add_argument('--smooth_lips', action='store_true', help="smooth the enc_a in a exponential decay way...")

    parser.add_argument('--torso', action='store_true', help="fix head and train torso")
    parser.add_argument('--head_ckpt', type=str, default='', help="head model")

    ### GUI options
    parser.add_argument('--gui', action='store_true', help="start a GUI")
    parser.add_argument('--W', type=int, default=450, help="GUI width")
    parser.add_argument('--H', type=int, default=450, help="GUI height")
    parser.add_argument('--radius', type=float, default=3.35, help="default GUI camera radius from center")
    parser.add_argument('--fovy', type=float, default=21.24, help="default GUI camera fovy")
    parser.add_argument('--max_spp', type=int, default=1, help="GUI rendering max sample per pixel")

    ### else
    parser.add_argument('--att', type=int, default=2, help="audio attention mode (0 = turn off, 1 = left-direction, 2 = bi-direction)")
    parser.add_argument('--aud', type=str, default='', help="audio source (empty will load the default, else should be a path to a npy file)")
    parser.add_argument('--emb', action='store_true', help="use audio class + embedding instead of logits")

    parser.add_argument('--ind_dim', type=int, default=4, help="individual code dim, 0 to turn off")
    parser.add_argument('--ind_num', type=int, default=10000, help="number of individual codes, should be larger than training dataset size")

    parser.add_argument('--ind_dim_torso', type=int, default=8, help="individual code dim, 0 to turn off")

    parser.add_argument('--amb_dim', type=int, default=2, help="ambient dimension")
    parser.add_argument('--part', action='store_true', help="use partial training data (1/10)")
    parser.add_argument('--part2', action='store_true', help="use partial training data (first 15s)")

    parser.add_argument('--train_camera', action='store_true', help="optimize camera pose")
    parser.add_argument('--smooth_path', action='store_true', help="brute-force smooth camera pose trajectory with a window size")
    parser.add_argument('--smooth_path_window', type=int, default=7, help="smoothing window size")

    # asr
    parser.add_argument('--asr', action='store_true', help="load asr for real-time app")
    parser.add_argument('--asr_wav', type=str, default='', help="load the wav and use as input")
    parser.add_argument('--asr_play', action='store_true', help="play out the audio")

    #parser.add_argument('--asr_model', type=str, default='deepspeech')
    parser.add_argument('--asr_model', type=str, default='cpierse/wav2vec2-large-xlsr-53-esperanto')
    # parser.add_argument('--asr_model', type=str, default='facebook/wav2vec2-large-960h-lv60-self')

    parser.add_argument('--push_url', type=str, default='rtmp://localhost/live/livestream')

    parser.add_argument('--asr_save_feats', action='store_true')
    # audio FPS
    parser.add_argument('--fps', type=int, default=50)
    # sliding window left-middle-right length (unit: 20ms)
    parser.add_argument('-l', type=int, default=10)
    parser.add_argument('-m', type=int, default=50)
    parser.add_argument('-r', type=int, default=10)

    parser.add_argument('--tts', type=str, default='edgetts') #xtts
    parser.add_argument('--ref_file', type=str, default=None)
    parser.add_argument('--xtts_server', type=str, default='http://localhost:9000')

    opt = parser.parse_args()
    app.config.from_object(opt)
    #print(app.config['xtts_server'])

    tts_type = opt.tts
    if tts_type == "xtts":
        print("Computing the latents for a new reference...")
        gspeaker = get_speaker(opt.ref_file, opt.xtts_server)

    # assert test mode
    opt.test = True
    opt.test_train = False
    #opt.train_camera =True
    # explicit smoothing
    opt.smooth_path = True
    opt.smooth_lips = True

    assert opt.pose != '', 'Must provide a pose source'

    # if opt.O:
    opt.fp16 = True
    opt.cuda_ray = True
    opt.exp_eye = True
    opt.smooth_eye = True

    opt.torso = True

    # assert opt.cuda_ray, "Only support CUDA ray mode."
    opt.asr = True

    if opt.patch_size > 1:
        # assert opt.patch_size > 16, "patch_size should > 16 to run LPIPS loss."
        assert opt.num_rays % (opt.patch_size ** 2) == 0, "patch_size ** 2 should be dividable by num_rays."
    seed_everything(opt.seed)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = NeRFNetwork(opt)

    criterion = torch.nn.MSELoss(reduction='none')
    metrics = [] # use no metric in GUI for faster initialization...
    print(model)
    trainer = Trainer('ngp', opt, model, device=device, workspace=opt.workspace, criterion=criterion, fp16=opt.fp16, metrics=metrics, use_checkpoint=opt.ckpt)

    test_loader = NeRFDataset_Test(opt, device=device).dataloader()
    model.aud_features = test_loader._data.auds
    model.eye_areas = test_loader._data.eye_area

    # we still need test_loader to provide audio features for testing.
    nerfreal = NeRFReal(opt, trainer, test_loader)
    #txt_to_audio('我是中国人,我来自北京')
    rendthrd = Thread(target=render)
    rendthrd.start()

    #############################################################################
    print('start websocket server')

    server = pywsgi.WSGIServer(('0.0.0.0', 8000), app, handler_class=WebSocketHandler)
    server.serve_forever()
    
    