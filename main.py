"""
ClawCamKeeper-skill 项目入口
工位摸鱼防护预警技能
"""

import sys
from pathlib import Path

# 确保项目根目录在路径中
sys.path.insert(0, str(Path(__file__).parent))

from cli.main import cli

if __name__ == '__main__':
    cli()
