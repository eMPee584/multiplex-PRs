import asyncio
import logging
import types
import uuid
from dataclasses import dataclass

import aiostream

from multiplex import ansi, to_iterator
from multiplex import keys
from multiplex import keys_input
from multiplex import resize
from multiplex import commands
from multiplex.actions import BoxAction
from multiplex.ansi import C, NONE
from multiplex.box import BoxHolder
from multiplex.enums import ViewLocation, BoxLine
from multiplex.exceptions import EndViewer
from multiplex.help import HelpViewState
from multiplex.iterator import Descriptor
from multiplex.refs import REDRAW, RECALC, SPLIT, QUIT, ALL_DOWN, OUTPUT_SAVED, SAVE

logger = logging.getLogger("multiplex.view")

MIN_BOX_HEIGHT = 7


class ViewerEvents:
    def __init__(self):
        self.queue = asyncio.Queue()

    async def receive(self):
        while True:
            yield await self.queue.get()
            self.queue.task_done()

    def send(self, message):
        self.queue.put_nowait(message)

    def send_redraw(self):
        self.queue.put_nowait((REDRAW, None))

    def send_quit(self):
        self.queue.put_nowait((QUIT, None))

    def send_all_down(self):
        self.queue.put_nowait((ALL_DOWN, None))

    def send_save(self):
        self.queue.put_nowait((SAVE, None))

    def send_output_saved(self):
        self.queue.put_nowait((OUTPUT_SAVED, None))


@dataclass
class DescriptorQueueItem:
    descriptor: Descriptor
    redraw: bool
    num_boxes: int


class Viewer:
    def __init__(self, descriptors, box_height, verbose, socket_path, output_path):
        self.holders = []
        self.stream_id_to_holder = {}
        self.holder_to_stream_id = {}
        self.input_reader = keys_input.InputReader(viewer=self, bindings=keys.bindings)
        self.descriptors_queue: "asyncio.Queue[DescriptorQueueItem]" = asyncio.Queue()
        self.help = HelpViewState(self)
        self.events = ViewerEvents()
        self.box_height = box_height
        self.verbose = verbose
        self.socket_path = socket_path
        self.output_path = output_path
        self.current_focused_box = 0
        self.current_view_line = 0
        self.maximized = False
        self.collaped_all = False
        self.wrapped_all = False
        self.cols = None
        self.lines = None
        self.stopped = False
        self.output_saved = False
        for i, descriptor in enumerate(descriptors):
            self.add(descriptor, redraw=False, num_boxes=len(descriptors))
            self.events.send_redraw()

    def add(self, descriptor, thread_safe=False, redraw=True, num_boxes=None):
        def action():
            self.descriptors_queue.put_nowait(
                DescriptorQueueItem(
                    descriptor=descriptor,
                    redraw=redraw,
                    num_boxes=num_boxes,
                )
            )

        if thread_safe:
            asyncio.get_event_loop().call_soon_threadsafe(action)
        else:
            action()

    def split(self, title, box_height=None):
        self.descriptors_queue.put_nowait(
            DescriptorQueueItem(
                descriptor=Descriptor(SPLIT, title, box_height),
                redraw=True,
                num_boxes=None,
            )
        )

    def swap_indices(self, index1, index2):
        holder1 = self.get_holder(index1)
        holder2 = self.get_holder(index2)
        self.holders[index1] = holder2
        self.holders[index2] = holder1
        holder1.index = index2
        holder2.index = index1
        if self.current_focused_box == index1:
            self.current_focused_box = index2
        elif self.current_focused_box == index2:
            self.current_focused_box = index1

    @property
    def num_boxes(self):
        return len(self.holders)

    @property
    def iterators(self):
        return [h.iterator for h in self.holders]

    @property
    def buffers(self):
        return [h.buffer for h in self.holders]

    @property
    def states(self):
        return [h.state for h in self.holders]

    @property
    def boxes(self):
        return [h.box for h in self.holders]

    def get_holder(self, index):
        return self.holders[index]

    def get_buffer(self, index):
        return self.holders[index].buffer

    def get_state(self, index):
        return self.holders[index].state

    def get_iterator(self, index):
        return self.holders[index].iterator

    def get_box(self, index):
        return self.holders[index].box

    async def run(self):
        try:
            self._setup()
            await self._main()
        finally:
            self.restore()
            self.stopped = True

    def _setup(self):
        loop = asyncio.get_event_loop()
        keys_input.setup()
        resize_notifier = resize.setup(self.events, loop)
        ansi.setup()
        return resize_notifier

    @staticmethod
    def restore():
        loop = asyncio.get_event_loop()
        keys_input.restore()
        resize.restore(loop)
        ansi.restore()

    async def _main(self):
        self._init()
        try:
            async with aiostream.stream.advanced.flatten(self._sources()).stream() as streamer:
                async for obj, output in streamer:
                    try:
                        await self._handle_event(obj, output)
                    except EndViewer:
                        return
        except RuntimeError as e:
            # not sure what's the cause, but occasionally this is raised during
            # aiostream context manager __aexit__, so we silently ignore
            if "StopAsyncIteration" not in str(e):
                raise

    async def _sources(self):
        yield self.input_reader.read()
        yield self.events.receive()
        while True:
            item = await self.descriptors_queue.get()
            source = await self._process_descriptor(item)
            if source:
                yield source
            self.descriptors_queue.task_done()

    async def _process_descriptor(self, descriptor_queue_item):
        descriptor = descriptor_queue_item.descriptor
        redraw = descriptor_queue_item.redraw
        recalc_num_boxes = descriptor_queue_item.num_boxes
        index = self.num_boxes
        stream_id = str(uuid.uuid4())
        iterator = await to_iterator(
            obj=descriptor.obj,
            title=descriptor.title,
            context={
                "socket_path": self.socket_path,
                "stream_id": stream_id,
            },
        )
        box_height = descriptor.box_height or self.box_height
        holder = BoxHolder(index, iterator=iterator, box_height=box_height, viewer=self)
        self.holders.append(holder)
        event = (REDRAW, None) if redraw else (RECALC, recalc_num_boxes)
        self.events.send(event)
        if redraw and not self.is_scrolling:
            self.events.send_all_down()
        if iterator.iterator is SPLIT:
            stream_id = self.holder_to_stream_id.pop(id(self.get_holder(index - 1)))
        self.holder_to_stream_id[id(holder)] = stream_id
        self.stream_id_to_holder[stream_id] = holder
        if iterator.iterator is not SPLIT:
            return self._wrapped_iterator(stream_id, iterator.iterator)

    @staticmethod
    async def _wrapped_iterator(stream_id, iterator):
        async for elem in iterator:
            yield stream_id, elem

    def _init(self):
        self._update_lines_cols()
        if not self.holders:
            return
        ansi.clear()
        self._update_holders()
        self._update_view()
        ansi.flush()

    def _update_holders(self, num_boxes=None):
        num_boxes = num_boxes or self.num_boxes
        default_box_height = max(MIN_BOX_HEIGHT, (self.lines - num_boxes - 1) // num_boxes)
        for holder in self.holders:
            holder.buffer.width = self.cols
            if not holder.state.changed_height:
                holder.state.box_height = default_box_height

    def _update_lines_cols(self):
        cols, lines = ansi.get_size()
        prev_cols = self.cols
        prev_lines = self.lines
        self.cols = cols
        self.lines = lines
        changed = prev_cols != self.cols or prev_lines != self.lines
        if changed:
            logger.debug(f"sizes: prev [{prev_lines}, {prev_cols}], new [{self.lines}, {self.cols}]")
        return changed

    async def _handle_event(self, obj, output):
        if obj is REDRAW:
            self._init()
            return
        if obj is RECALC:
            self._update_holders(output)
            return
        if obj is QUIT:
            raise EndViewer

        if obj is SAVE:
            await commands.save(self)
            self._update_status_bar()
        elif obj is OUTPUT_SAVED:
            await asyncio.sleep(0.1)
            self.output_saved = False
            self._update_status_bar()
        elif obj is ALL_DOWN:
            commands.all_down(self)
            ansi.clear()
            self._update_view()
        elif isinstance(obj, str):
            holder = self.stream_id_to_holder[obj]
            self._update_box(holder.index, output)
            if isinstance(output, BoxAction) or callable(output):
                ansi.clear()
                self._update_view()
        else:
            key_changed = False
            boxes_changed = set()
            full_refresh = False
            for fn in output:
                current_key_changed = await self._process_key_handler(fn)
                if current_key_changed is ansi.FULL_REFRESH:
                    full_refresh = True
                    key_changed = True
                elif type(current_key_changed) == int:
                    boxes_changed.add(current_key_changed)
                else:
                    key_changed = key_changed or current_key_changed
            if full_refresh:
                ansi.clear()
            if key_changed:
                self._update_view()
            elif boxes_changed:
                self._update_status_bar()
                for index in boxes_changed:
                    self._update_box(index, data=None)
            else:
                self._update_status_bar()
        ansi.flush()

    def _update_box(self, i, data):
        if data is not None:
            if isinstance(data, BoxAction):
                data.run(self.get_holder(i))
            elif callable(data):
                data(self.get_holder(i))
            else:
                self.get_buffer(i).write(data)
        if self.help.show:
            return
        self._update_title_line(i)
        box = self.get_box(i)
        if box.is_visible:
            box.update()

    async def _process_key_handler(self, fn):
        prev_current_line = self.current_view_line
        prev_focused_box = self.current_focused_box
        update_view = fn(self)
        if isinstance(update_view, types.CoroutineType):
            update_view = await update_view
        if update_view is not None and not isinstance(update_view, bool):
            return update_view
        if prev_focused_box != self.current_focused_box:
            return ansi.FULL_REFRESH
        if prev_current_line != self.current_view_line:
            return ansi.FULL_REFRESH
        return update_view

    def _update_view(self):
        if self.help.show:
            ansi.help_screen(
                current_line=self.help.current_line,
                lines=self.lines,
                cols=self.cols - 1,
                descriptions=keys.descriptions,
            )
        else:
            self._update_status_bar()
            self._update_title_lines()
            self._update_boxes()

    def _update_boxes(self):
        if self.maximized:
            self._update_box(self.current_focused_box, data=None)
        else:
            for i in range(self.num_boxes):
                self._update_box(i, data=None)

    def _update_title_lines(self):
        if self.maximized:
            self._update_title_line(self.current_focused_box)
        else:
            for i in range(self.num_boxes):
                self._update_title_line(i)

    def _update_title_line(self, index):
        screen_y, location = self.get_title_line(index)
        if location != ViewLocation.IN_VIEW:
            return

        iterator = self.get_iterator(index)
        suffix = ""
        if self.verbose:
            box_state = self.get_state(index)
            wrap = box_state.wrap
            auto_scroll = box_state.auto_scroll
            collapsed = box_state.collapsed
            buffer_line = box_state.buffer_start_line
            box_height = box_state.box_height
            state = f"{'W' if wrap else '-'}{'-' if auto_scroll else 'S'}{'C' if collapsed else '-'}"
            state = f"{state} [{buffer_line},{box_height}]"
            suffix = f" [{state}]"
        suffix_len = len(suffix)
        title = iterator.title
        if not isinstance(title, C):
            title = C(title)
        title_len = len(title)
        hr_space = 4
        _ellipsis = "..."
        if hr_space + title_len + suffix_len > self.cols:
            title = title[: self.cols - suffix_len - len(_ellipsis) - hr_space]
            title += _ellipsis
        text = C(" ", title, suffix, " ", fg=NONE, bg=NONE)

        logger.debug(f"s{index}:\t{screen_y}\t{location}\t[{self.lines},{self.cols}]")

        hline_color = ansi.theme.TITLE_FOCUS if index == self.current_focused_box else ansi.theme.TITLE_NORMAL
        ansi.title(
            row=screen_y,
            text=text,
            hline_color=hline_color,
            cols=self.cols,
        )

    def _update_status_bar(self):
        if self.help.show:
            return

        focused = self.focused
        iterator = self.get_iterator(focused.index)
        title = iterator.title
        box_state = focused.state
        wrap = box_state.wrap
        auto_scroll = box_state.auto_scroll

        modes = []
        if not auto_scroll:
            modes.append("SCROLL")
        if self.maximized:
            modes.append("MAXIMIZED")
        if wrap:
            modes.append("WRAP")
        mode = "|".join(modes)
        mode_paren = f"({mode})" if mode else ""

        pending_keys = self.input_reader.pending
        pending_name_parts = []
        while pending_keys:
            cur_offset = len(pending_keys)
            name = None
            while not name and cur_offset:
                name = keys.seq_to_name(pending_keys[:cur_offset], fallback=False)
                if not name:
                    cur_offset -= 1
            pending_name_parts.append(name)
            pending_keys = pending_keys[cur_offset:]

        pending_text = "".join(pending_name_parts)
        if pending_text:
            pending_text = f"{pending_text} "

        if not isinstance(title, C):
            title = C(title)
        title_len = len(title)
        mode_len = len(mode_paren)
        pending_len = len(pending_text)
        space_between = self.cols - title_len - mode_len - pending_len
        if space_between < 0:
            _ellipsis = "... "
            title = title[: (self.cols - mode_len) - len(_ellipsis)]
            title += _ellipsis
            space_between = 0
        if self.output_saved:
            color = ansi.theme.STATUS_SAVE
        elif not auto_scroll:
            color = ansi.theme.STATUS_SCROLL
        else:
            color = ansi.theme.STATUS_NORMAL
        text = C(title, " " * space_between, pending_text, mode_paren, fg=color[0], bg=color[1])

        ansi.status_bar(
            row=self.get_status_bar_line(),
            text=text,
        )

    def verify_focused_box_in_view(self):
        if self.maximized:
            return
        screen_y, location = self.get_box_bottom_line(self.current_focused_box)
        if location == ViewLocation.BELOW:
            offset = screen_y
            self.current_view_line += offset
            return
        screen_y, location = self.get_title_line(self.current_focused_box)
        if location == ViewLocation.ABOVE:
            offset = screen_y
            self.current_view_line -= offset
        elif location == ViewLocation.BELOW:
            offset = screen_y
            self.current_view_line += offset

    @property
    def focused(self):
        return self.get_box(self.current_focused_box)

    @property
    def is_scrolling(self):
        return not self.focused.state.auto_scroll

    @property
    def max_current_line(self):
        result = 0
        for state in self.states:
            result += 1
            if not state.collapsed:
                result += state.box_height
        result -= self.get_max_box_line()
        return max(0, result)

    def get_status_bar_line(self):
        return self.lines - 1

    def get_max_box_line(self):
        return self.lines - 2

    def get_title_line(self, index):
        return self._get_box_line(index, BoxLine.TITLE)

    def get_box_top_line(self, index):
        return self._get_box_line(index, BoxLine.TOP)

    def get_box_bottom_line(self, index):
        return self._get_box_line(index, BoxLine.BOTTOM)

    def _get_box_line(self, index, box_line):
        if self.maximized:
            if index != self.current_focused_box:
                return 0, ViewLocation.NOT_FOCUSED
            if box_line == BoxLine.TITLE:
                screen_y = 0
            elif box_line == BoxLine.TOP:
                screen_y = 1
            elif box_line == BoxLine.BOTTOM:
                screen_y = self.get_max_box_line()
            else:
                raise RuntimeError(box_line)
            return screen_y, ViewLocation.IN_VIEW
        else:
            state = self.get_state(index)
            if state.collapsed and box_line != BoxLine.TITLE:
                return 0, ViewLocation.NOT_FOCUSED
            view_y = 0
            for i in range(index):
                # title line
                view_y += 1
                other_state = self.states[i]
                if not other_state.collapsed:
                    view_y += other_state.box_height
            if box_line == BoxLine.TOP:
                view_y += 1
            elif box_line == BoxLine.BOTTOM:
                view_y += state.box_height
            screen_y = view_y - self.current_view_line
            max_line = self.get_max_box_line()
            if screen_y > max_line:
                offest = screen_y - max_line
                return offest, ViewLocation.BELOW
            elif screen_y < 0:
                offset = abs(screen_y)
                return offset, ViewLocation.ABOVE
            else:
                return screen_y, ViewLocation.IN_VIEW
