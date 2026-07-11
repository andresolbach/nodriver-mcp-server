"""
Interactive terminal UI for the installer.
"""

import sys


def _make_read_key():
    if not sys.stdin.isatty():
        return None
    try:
        if sys.platform == "win32":
            import msvcrt

            def read_key():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    ch2 = msvcrt.getwch()
                    if ch2 == "H":
                        return "up"
                    if ch2 == "P":
                        return "down"
                    return None
                if ch == " ":
                    return "space"
                if ch == "\r":
                    return "enter"
                if ch == "\x1b":
                    return "esc"
                if ch == "a":
                    return "a"
                return None
        else:
            import termios
            import tty

            def read_key():
                fd = sys.stdin.fileno()
                old = termios.tcgetattr(fd)
                try:
                    tty.setraw(fd)
                    ch = sys.stdin.read(1)
                    if ch == "\x1b":
                        ch2 = sys.stdin.read(1)
                        if ch2 == "[":
                            ch3 = sys.stdin.read(1)
                            if ch3 == "A":
                                return "up"
                            if ch3 == "B":
                                return "down"
                        return "esc"
                    if ch == " ":
                        return "space"
                    if ch in ("\r", "\n"):
                        return "enter"
                    if ch == "a":
                        return "a"
                    if ch == "\x03":
                        return "esc"
                    return None
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)

        return read_key
    except ImportError:
        return None


def _tui_loop(read_key, render, on_key) -> bool:
    sys.stdout.write("\033[?25l")
    output = render()
    sys.stdout.write(output + "\n")
    sys.stdout.flush()
    total_lines = output.count("\n") + 1

    def clear():
        sys.stdout.write(f"\033[{total_lines}A\033[J")
        sys.stdout.flush()

    try:
        while True:
            key = read_key()
            result = on_key(key)
            if result == "confirm":
                clear()
                return True
            if result == "cancel":
                clear()
                return False
            if result == "noop":
                continue
            clear()
            output = render()
            sys.stdout.write(output + "\n")
            sys.stdout.flush()
            total_lines = output.count("\n") + 1
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()


def interactive_select(items: list[tuple[str, bool]], title: str, *, show_status: bool = False) -> list[str] | None:
    read_key = _make_read_key()
    if read_key is None:
        return None

    selected = [False] * len(items)
    cursor = 0

    # Track which items have the status flag (i.e. installed)
    has_status = [checked for _, checked in items]

    def render():
        lines = [f"\033[1m{title}\033[0m"]
        lines.append("  (space: toggle, a: select/deselect all, enter: confirm, esc: cancel)")
        lines.append("")
        for i, (name, _) in enumerate(items):
            check = "\033[32m[x]\033[0m" if selected[i] else "[ ]"
            pointer = "\033[36m>\033[0m" if i == cursor else " "
            badge = " \033[33m(installed)\033[0m" if show_status and has_status[i] else ""
            lines.append(f"  {pointer} {check} {name}{badge}")
        return "\n".join(lines)

    def on_key(key):
        nonlocal cursor, selected
        if key == "up":
            cursor = (cursor - 1) % len(items)
        elif key == "down":
            cursor = (cursor + 1) % len(items)
        elif key == "space":
            selected[cursor] = not selected[cursor]
        elif key == "a":
            all_sel = all(selected)
            selected[:] = [not all_sel] * len(items)
        elif key == "enter":
            return "confirm"
        elif key == "esc":
            return "cancel"
        else:
            return "noop"
        return "redraw"

    if _tui_loop(read_key, render, on_key):
        result = [name for (name, _), sel in zip(items, selected) if sel]
        if result:
            print(f"\033[1m{title}\033[0m {', '.join(result)}")
        else:
            print(f"\033[1m{title}\033[0m (none)")
        return result
    return None
