# 三种制作方式

默认进入「企业／沙龙宣传 → 沙龙活动宣传」。选择视频素材，填写想法，点击「按想法写讲稿」，阅读并采用或直接修改独白，然后生成成片。

- 独白宣传、企业／沙龙宣传：GPU Qwen3-TTS 中文配音，实际语音长度决定成片长度。支持分句中文字幕、轻音乐、低音量原声。当前按素材选择顺序组织画面，不会理解画面并自动匹配讲稿。
- 人物口播：保存公共合成形象或本人已授权照片，使用 MuseTalk 1.5 同步嘴型。必须完成真实 GPU 生成验收，能力才显示可用。身体静态；服装、背景与所选形象一致。任意换装、背景替换和全身动作尚未接通。
- 只剪原视频：保留原有素材剪辑流程。

创意方案与实际讲稿分别保存。生成讲稿不会自动创建视频或覆盖手写独白。断线后使用原任务编号核对；视频完成后仍进入人工审片。

## 租用 GPU 运行环境

网页和文件上传入口运行在本机。讲稿模型、中文配音和人物嘴型在租用 GPU 上运行；本版本不调用 HeyGen。GPU 模型位于数据盘，Python 环境分开，避免改动原有镜像。

声音模型部署目录须包含完整的语音 tokenizer。人物模型必须先通过 `studio.presenter.validate_runtime` 实际音画检查；文件存在本身不等于可以生成。

## 模型来源与许可记录

- Qwen3-4B-Instruct-2507、Qwen3-TTS：Qwen 官方模型；Apache 2.0。https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
- MuseTalk 1.5：官方代码 MIT，项目说明允许模型商用。https://github.com/TMElyralab/MuseTalk
- 人脸定位采用 OpenCV YuNet 官方 MIT 模型。https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet
- SD VAE 与 Whisper：分别采用官方模型及随附许可。https://huggingface.co/stabilityai/sd-vae-ft-mse 与 https://huggingface.co/openai/whisper-tiny
- 公共合成人像如使用 SDXL，保存生成模型、种子和 CreativeML Open RAIL++-M 许可来源。https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0

人物适配器不使用原 MuseTalk 的 BiSeNet、S3FD、DWPose 或 InsightFace 权重；采用 YuNet 正脸定位与几何羽化嘴部合成。商业上线前仍需按最终素材、人物授权和模型许可审阅成片。未自动发布或发送任何测试作品。
