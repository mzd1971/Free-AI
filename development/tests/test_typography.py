"""Native font changes keep widget contents and Windows DPI settings intact."""
import tempfile
import tkinter as tk
from tkinter import font as tkfont, ttk
import unittest
from pathlib import Path

from ui_kit import (
    CYBER_COLORS, DARK_COLORS, LIGHT_COLORS, FlatButton, ScrollArea, label, set_text_size, setup_theme, ui_font,
)
from ui_preferences import load_text_size, save_text_size


class TypographyTests(unittest.TestCase):
    def test_shared_preference_round_trip_and_invalid_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            path = Path(directory) / 'text-size.txt'
            self.assertEqual(load_text_size(directory), 'normal')
            save_text_size('large', directory)
            self.assertEqual(load_text_size(directory), 'large')
            with self.assertRaises(ValueError):
                save_text_size('unknown', directory)
            self.assertEqual(load_text_size(directory), 'large')
            path.write_bytes(b'\xff\xff')
            self.assertEqual(load_text_size(directory), 'normal')
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_native_controls_and_tags_resize_without_rebuilding(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        dpi = float(root.tk.call('tk', 'scaling'))
        setup_theme(root, 'Typography test')
        root.geometry('1000x700+2500+1400')
        entry = ttk.Entry(root)
        entry.pack()
        entry.insert(0, '当前草稿')
        heading = label(root, '标题', size=16, bold=True)
        heading.pack()
        text = tk.Text(root, height=3, font=ui_font(root))
        text.pack()
        text.tag_configure('code', font=ui_font(text, 11, family='Consolas'))
        text.insert('1.0', 'print("中文草稿")', 'code')
        text.mark_set('insert', '1.4')
        button = FlatButton(root, '保存当前草稿', width=112, height=28)
        button.pack()
        root.update()

        def size(name):
            return tkfont.nametofont(str(name), root=root).cget('size')

        self.assertEqual(size(entry.cget('font')), 12)
        initial_height = button.winfo_reqheight()
        set_text_size(root, 'extra_large')
        root.update()
        self.assertEqual(size(entry.cget('font')), 16)
        self.assertEqual(size(heading.cget('font')), 21)
        self.assertEqual(size(text.tag_cget('code', 'font')), 15)
        self.assertEqual(text.get('1.0', 'end-1c'), 'print("中文草稿")')
        self.assertEqual(text.index('insert'), '1.4')
        self.assertEqual(entry.get(), '当前草稿')
        self.assertGreater(button.winfo_reqheight(), initial_height)
        self.assertGreaterEqual(button.winfo_reqwidth(), 112)
        late = label(root, '新建控件')
        self.assertEqual(size(late.cget('font')), 16)
        set_text_size(root, 'large')
        self.assertEqual(size(late.cget('font')), 14)
        set_text_size(root, 'normal')
        root.update()
        self.assertEqual(size(late.cget('font')), 12)
        self.assertEqual(button.winfo_reqheight(), initial_height)
        self.assertEqual(float(root.tk.call('tk', 'scaling')), dpi)

    def test_scroll_area_centers_content_and_keeps_wheel_region(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        area = ScrollArea(root, max_width=600, scrollbar=True)
        area.pack(fill='both', expand=True)
        for index in range(30):
            label(area.body, str(index), size=18).pack(pady=9)
        root.geometry('900x300+2500+1400')
        root.update()
        self.assertEqual(area.body.winfo_width(), 600)
        self.assertEqual(area.canvas.coords(area.window)[0], (area.canvas.winfo_width() - 600) // 2)
        area.canvas.yview_moveto(1)
        self.assertEqual(area.canvas.yview()[1], 1.0)
        root.geometry('450x300+2500+1400')
        root.update()
        self.assertEqual(area.body.winfo_width(), area.canvas.winfo_width())
        self.assertEqual(area.canvas.coords(area.window)[0], 0)

    def test_readonly_values_and_button_labels_fit_at_desktop_dpi(self):
        for theme, colors in (('light', LIGHT_COLORS), ('dark', DARK_COLORS), ('cyberpunk', CYBER_COLORS)):
            for dpi in (1.0, 1.5, 2.0):
                with self.subTest(theme=theme, dpi=dpi):
                    root = tk.Tk()
                    try:
                        root.tk.call('tk', 'scaling', dpi)
                        setup_theme(root, 'Control size test', theme=theme)
                        root.geometry('1000x700+2500+1400')
                        choice = tk.StringVar(root, '标准')
                        picker = ttk.Combobox(root, values=('标准', '大', '特大'), textvariable=choice,
                                              width=6, state='readonly')
                        picker.pack()
                        button = FlatButton(root, '反馈并存为待确认建议', width=100, height=28)
                        button.pack()
                        style = ttk.Style(root)
                        self.assertEqual(style.lookup('TCombobox', 'foreground', ('readonly',)), colors['text'])
                        self.assertEqual(style.lookup('TCombobox', 'fieldbackground', ('readonly',)), colors['input'])
                        self.assertEqual(style.lookup('TCombobox', 'selectforeground', ('readonly',)), colors['selection_fg'])
                        self.assertEqual(style.lookup('TCombobox', 'selectbackground', ('readonly',)), colors['selection_bg'])

                        for size, expected in (('normal', '标准'), ('extra_large', '特大')):
                            set_text_size(root, size)
                            choice.set(expected)
                            root.update()
                            self.assertEqual(picker.get(), expected)
                            x, y, width, height = map(int, root.tk.splitlist(root.tk.call(picker, 'bbox', 1)))
                            self.assertGreater(width, 0)
                            self.assertGreater(height, 0)
                            self.assertLess(x + width, picker.winfo_width())
                            self.assertLess(y + height, picker.winfo_height())
                            text = next(item for item in button.find_all() if button.type(item) == 'text')
                            self.assertEqual(button.itemcget(text, 'text'), button.cget('text'))
                            left, top, right, bottom = button.bbox(text)
                            self.assertGreaterEqual(left, 0)
                            self.assertGreaterEqual(top, 0)
                            self.assertLessEqual(right, button.winfo_width())
                            self.assertLessEqual(bottom, button.winfo_height())
                    finally:
                        root.destroy()

    def test_variable_caption_can_opt_into_a_width_cap(self):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        caption = '一个用户创建的很长的项目文件夹名称'
        bounded = FlatButton(root, '选择文件夹', width=92, max_width=126)
        bounded.pack()
        bounded.configure(text=caption)
        root.update()
        self.assertEqual(bounded.winfo_reqwidth(), 126)
        self.assertEqual(bounded.cget('text'), caption)
        text = next(item for item in bounded.find_all() if bounded.type(item) == 'text')
        self.assertTrue(bounded.itemcget(text, 'text').endswith('…'))
