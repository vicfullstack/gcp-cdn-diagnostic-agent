import sys
import os

# 将项目根目录添加到 python 搜索路径中，以便动态载入根目录下的 agent.py
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 引入根目录下的主 root_agent，确保全局只有一份代码源（Single Source of Truth）
from agent import root_agent
