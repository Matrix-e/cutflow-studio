"""Ask only this project's GPU bridge to stop itself; never kill arbitrary PIDs."""
from pathlib import Path
from start_local import environment

data=Path(environment()['CUTFLOW_DATA'])
data.mkdir(parents=True,exist_ok=True)
(data/'gpu-client.stop').touch()
print('已请求停止本项目的本机同步与转发。远端服务和租用实例不会关闭。')
