from graia.ariadne.message.element import Image

import re

from loguru import logger

from constants import config
from utils.text_to_img import to_image

from typing import Optional, Any
import time

from graia.ariadne.message.chain import MessageChain
from graia.ariadne.message.element import Plain


class Renderer:
    context = {}

    async def render(self, msg: str): ...

    async def result(self): ...

    async def __aenter__(self): ...

    async def __aexit__(self, exc_type: type[BaseException], exc: BaseException, tb): ...


class FullTextRenderer(Renderer):
    msg: str

    async def render(self, msg: str) -> None:
        self.msg = msg
        return None

    async def result(self) -> str:
        return str(self.msg)

    async def __aenter__(self) -> None:
        self.msg = ''

    async def __aexit__(self, exc_type: type[BaseException], exc: BaseException, tb) -> None:
        self.msg = None


class MarkdownImageRenderer(Renderer):
    msg: str

    async def render(self, msg: str) -> None:
        self.msg = msg
        return None

    async def result(self) -> Image:
        return await to_image(str(self.msg))

    async def __aenter__(self) -> None:
        self.msg = ''

    async def __aexit__(self, exc_type: type[BaseException], exc: BaseException, tb) -> None:
        self.msg = None


class MultipleSegmentRenderer(Renderer):
    last_commit: str = ''
    uncommitted_msg: str = ''

    async def render(self, msg: str) -> Optional[str]:
        self.uncommitted_msg = msg.removeprefix(self.last_commit)
        segments = self.uncommitted_msg.strip().split("\n")
        # logger.debug("收到 uncommited_msg:" + str(self.uncommitted_msg))
        # Skip empty message
        if self.uncommitted_msg.strip() == '':
            self.last_commit = msg
            return None
        # Merge code
        if segments[0].startswith("```"):
            if len(segments) == 1:
                # Waiting for more line
                return None
            tokens = segments[0]
            for i, seg in enumerate(segments[1:]):
                tokens = tokens + '\n' + seg
                if seg.endswith("```"):
                    # Keep left
                    self.last_commit = self.last_commit + \
                                       self.uncommitted_msg[:len(tokens) + self.uncommitted_msg.find(tokens)]
                    return tokens
            else:
                return None
        # Merge Tex
        elif segments[0].startswith("$$"):
            if len(segments) == 1:
                # Waiting for more line
                return None
            tokens = segments[0]
            for i, seg in enumerate(segments[1:]):
                tokens = tokens + '\n' + seg
                if seg.endswith("$$"):
                    # Keep left
                    self.last_commit = self.last_commit + \
                                       self.uncommitted_msg[:len(tokens) + self.uncommitted_msg.find(tokens)]
                    return tokens
            else:
                return None
        # Merge Lists
        elif segments[0].startswith("* "):
            if segments[-1] == '' or segments[-1].startswith("*"):
                return None
            else:
                self.last_commit = msg.removesuffix(segments[-1])
                return self.uncommitted_msg.removesuffix(segments[-1]).strip()
        # Direct segments
        elif self.uncommitted_msg[-1] == '\n':
            self.last_commit = msg
            # logger.debug("直接发送消息：" + '\n'.join(segments).strip())
            return '\n'.join(segments).strip()
        return None

    async def result(self) -> str:
        return self.uncommitted_msg

    async def __aenter__(self) -> None:
        self.msg = ''

    async def __aexit__(self, exc_type: type[BaseException], exc: BaseException, tb) -> None:
        self.msg = None


class BufferedContentRenderer(Renderer):
    last_arrived = None
    hold = None

    def __init__(self, parent: Renderer):
        self.parent = parent

    async def __aenter__(self) -> None:
        self.hold = []
        self.last_arrived = time.time()
        await self.parent.__aenter__()

    async def __aexit__(self, exc_type: type[BaseException], exc: BaseException, tb) -> None:
        self.hold = None
        await self.parent.__aexit__(exc_type, exc, tb)

    async def render(self, msg: str) -> Optional[Any]:
        rendered = await self.parent.render(msg)
        if not rendered:
            return None
        # logger.debug("上一级渲染结果：" + str(rendered))
        current_time = time.time()
        time_delta = current_time - self.last_arrived
        self.hold.append(Plain(rendered + '\n'))
        if time_delta < config.response.buffer_delay:
            return None
        if self.hold:
            self.last_arrived = current_time
            rendered = MessageChain(self.hold)
            self.hold = []
            # logger.debug("缓冲时间已到，输出渲染文本：" + str(rendered))
            return rendered
        return None

    async def result(self) -> Optional[Any]:
        result = MessageChain([])
        if self.hold:
            result = result + MessageChain(self.hold)
        if parent := await self.parent.result():
            result = result + parent
        if len(result) > 0:
            # fg("输出剩余内容：" + result)
            return result
        return None


class MixedContentMessageChainRenderer(Renderer):

    def __init__(self, parent: Renderer):
        self.parent = parent

    async def __aenter__(self) -> None:
        await self.parent.__aenter__()

    async def __aexit__(self, exc_type: type[BaseException], exc: BaseException, tb) -> None:
        await self.parent.__aexit__(exc_type, exc, tb)

    def is_rich_content(self, input_str: str):
        # Regular expressions to search for Markdown or LaTeX patterns
        markdown_pattern = r"(\*\*|__|\*|_|\[|\]|\(|\)|#|\+|\-|`|~|>|!)\w*(\*\*|__|\*|_|\[|\]\(|\(|\)|#|\+|\-|`|~|>|!)"
        latex_pattern = r"\$(.*?)\$"

        # Search for Markdown or LaTeX patterns in the input string
        if re.search(markdown_pattern, input_str) or re.search(latex_pattern, input_str):
            return True
        else:
            return False

    async def parse(self, groups: Optional[MessageChain]) -> Optional[MessageChain]:
        if not groups:
            return None
        holds = []
        rich_blocks = ''
        plain_blocks = ''
        # 合并同类项
        for rendered in groups:
            if not str(rendered).strip():
                continue
            if self.is_rich_content(str(rendered)):
                if plain_blocks.strip():
                    holds.append(Plain(plain_blocks.strip()))
                    plain_blocks = ''
                rich_blocks = rich_blocks + str(rendered) + '\n'
            else:
                if rich_blocks.strip():
                    holds.append(await to_image(rich_blocks.strip()))
                    rich_blocks = ''
                plain_blocks = plain_blocks + str(rendered)
        # 判断最后一项，把剩余的东西丢进去
        if rich_blocks.strip():
            holds.append(await to_image(rich_blocks))
        if plain_blocks.strip():
            holds.append(Plain(plain_blocks))
        if holds:
            return MessageChain(holds)
        return None

    async def render(self, msg: str) -> Optional[MessageChain]:
        return await self.parse(await self.parent.render(msg))

    async def result(self) -> Optional[MessageChain]:
        return await self.parse(await self.parent.result())