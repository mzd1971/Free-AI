"""Native switch interactions, preference updates, and responsive layout."""

import tkinter as tk
import unittest

from ui_controls import SwitchRow
from ui_kit import CLIENT_LIGHT_COLORS, COLORS, set_text_size, setup_theme


class SwitchRowTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.addCleanup(self.root.destroy)
        self.root.withdraw()
        self.root.tk.call('tk', 'scaling', 4 / 3)
        setup_theme(self.root, 'SwitchRow test', min_size=(1, 1), palette=CLIENT_LIGHT_COLORS)
        self.root.geometry('360x580+9000+9000')
        self.root.deiconify()

    def test_external_variable_updates_and_destruction(self):
        initial = tk.BooleanVar(self.root, False)
        calls = []
        row = SwitchRow(self.root, '本地记忆', initial, lambda: calls.append(initial.get()))
        row.pack(fill='x')
        self.root.update()

        self.assertEqual(row.cget('variable'), str(initial))
        self.assertEqual(row._track.itemcget('track', 'fill'), '#b6c1d0')
        thumb_off = row._track.coords('thumb')
        initial.set(True)
        self.assertEqual(calls, [])
        self.assertEqual(row._track.itemcget('track', 'fill'), COLORS['primary_bg'])
        self.assertGreater(row._track.coords('thumb')[0], thumb_off[0])
        self.assertEqual(row._track.itemcget('thumb', 'fill'), '#ffffff')
        row.invoke()
        self.assertEqual(calls, [False])

        replacement = tk.BooleanVar(self.root, True)
        row.config({'variable': replacement, 'text': '允许记录', 'description': '仅在这台电脑生效。'},
                   bg=COLORS['side'], command=lambda: 'saved')
        self.root.update()
        self.assertEqual(row.cget('text'), '允许记录')
        self.assertEqual(row.cget('variable'), str(replacement))
        self.assertEqual(row._track.cget('bg'), COLORS['side'])
        self.assertTrue(row._description.winfo_ismapped())
        self.assertEqual(initial.trace_info(), [])
        self.assertEqual(row.invoke(), 'saved')
        self.assertFalse(replacement.get())
        row.configure(description='')
        self.assertFalse(row._description.winfo_manager())
        row.destroy()
        self.assertEqual(replacement.trace_info(), [])
        replacement.set(True)

    def test_dark_switch_keeps_white_thumb_clear_of_track(self):
        self.root.withdraw()
        setup_theme(self.root, 'Dark switch test', theme='dark', min_size=(1, 1))
        self.root.geometry('360x580+9000+9000')
        self.root.deiconify()
        variable = tk.BooleanVar(self.root, False)
        row = SwitchRow(self.root, '本地记忆', variable)
        row.pack(fill='x')
        self.root.update()

        self.assertEqual(row._track.itemcget('track', 'fill'), '#626b79')
        variable.set(True)
        self.assertEqual(row._track.itemcget('track', 'fill'), '#4778c6')
        self.assertEqual(row._track.itemcget('thumb', 'fill'), '#ffffff')

    def test_mouse_keyboard_focus_and_disabled_state(self):
        before = tk.Entry(self.root)
        before.pack()
        variable = tk.BooleanVar(self.root, False)
        calls = []
        row = SwitchRow(self.root, '允许联网', variable, lambda: calls.append(variable.get()),
                        description='此选项会影响联网模式。')
        row.pack(fill='x')
        after = tk.Entry(self.root)
        after.pack()
        self.root.update()
        before.focus_force()
        before.event_generate('<KeyPress-Tab>')
        self.root.update()

        self.assertIs(self.root.focus_get(), row)
        self.assertEqual(row._track.itemcget('focus', 'outline'), COLORS['focus'])
        for _ in range(4):
            row.event_generate('<KeyPress-space>')
        self.assertFalse(variable.get())
        row.event_generate('<KeyRelease-space>')
        self.assertEqual(calls, [True])
        row.event_generate('<KeyPress-Return>')
        row.event_generate('<KeyRelease-Return>')
        self.assertEqual(calls, [True, False])

        for surface in (row._title, row._description, row._track):
            surface.event_generate('<ButtonPress-1>', x=8, y=8)
            surface.event_generate('<ButtonRelease-1>', x=8, y=8)
        self.assertEqual(calls, [True, False, True, False, True])
        row._track.event_generate('<ButtonPress-1>', x=8, y=8)
        row._track.event_generate('<ButtonRelease-1>', x=8, y=-70)
        self.assertEqual(len(calls), 5)

        row.configure(state='disabled')
        self.assertEqual(row.cget('state'), 'disabled')
        self.assertTrue(row.instate(['disabled']))
        self.assertFalse(row.instate(['!disabled']))
        self.assertFalse(row._track.find_withtag('focus'))
        row.invoke()
        row._track.event_generate('<ButtonPress-1>', x=8, y=8)
        row._track.event_generate('<ButtonRelease-1>', x=8, y=8)
        self.assertEqual(len(calls), 5)
        self.assertTrue(variable.get())
        variable.set(False)
        self.assertEqual(row._track.itemcget('track', 'fill'), COLORS['disabled_bg'])
        self.assertEqual(len(calls), 5)

        before.focus_force()
        before.event_generate('<KeyPress-Tab>')
        self.root.update()
        self.assertIs(self.root.focus_get(), after)
        row.configure(state='normal')
        self.assertTrue(row.instate(['!disabled']))

    def test_narrow_large_text_wraps_without_shrinking_track(self):
        self.root.geometry('190x580+9000+9000')
        row = SwitchRow(self.root, '允许联网模型读取长期记忆', tk.BooleanVar(self.root),
                        description='获准后，相关记忆可随请求发送给你选用的服务商。')
        row.pack(fill='x')
        self.root.update()
        initial_height = row.winfo_height()
        track_width = row._track.winfo_width()

        set_text_size(self.root, 'extra_large')
        self.root.update()
        self.assertGreater(row.winfo_height(), initial_height)
        self.assertEqual(row._track.winfo_width(), track_width)
        self.assertEqual(row._track.winfo_reqwidth(), track_width)
        self.assertLess(row._labels.winfo_rootx() + row._labels.winfo_width(), row._track.winfo_rootx())
        for widget in (row._title, row._description):
            self.assertLessEqual(int(widget.cget('wraplength')), widget.winfo_width())
            self.assertEqual(widget.winfo_height(), widget.winfo_reqheight())

        self.root.geometry('420x580+9000+9000')
        self.root.update()
        self.assertLess(row.winfo_height(), initial_height)
        self.assertGreaterEqual(row.winfo_height(), 44)


if __name__ == '__main__':
    unittest.main()
