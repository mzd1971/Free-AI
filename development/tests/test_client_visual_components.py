"""Vector button icons retain their layout, commands, and theme colors."""
import tkinter as tk
import unittest
from unittest.mock import patch

from ui_kit import (
    CLIENT_LIGHT_COLORS, COLORS, FlatButton, RoundedFrame,
    display_scale, set_text_size, setup_theme, text_scale, ui_font,
)


class ClientVisualComponentTests(unittest.TestCase):
    def create_root(self, theme='light', *, dpi=1.25, layout_scale=None):
        root = tk.Tk()
        self.addCleanup(root.destroy)
        root.tk.call('tk', 'scaling', dpi)
        if layout_scale is not None:
            root._free_ai_display_scale = layout_scale
        setup_theme(root, 'Vector button test', theme=theme,
                    palette=CLIENT_LIGHT_COLORS if theme == 'light' else None)
        root.geometry('640x360+9000+9000')
        return root

    def test_each_icon_draws_inside_an_icon_only_button(self):
        root = self.create_root()
        names = (
            'sidebar', 'plus', 'folder', 'grid', 'sparkle', 'memory', 'settings',
            'chevron_down', 'more', 'arrow_up', 'stop', 'copy', 'panel',
        )
        button = FlatButton(root, width=34, height=34, font_size=11, icon='plus')
        button.pack()
        root.update()
        for icon in names:
            with self.subTest(icon=icon):
                button.configure(icon=icon)
                root.update()
                self.assertEqual(button.cget('icon'), icon)
                self.assertEqual(button.winfo_reqwidth(), 34)
                self.assertFalse(any(button.type(item) == 'text' for item in button.find_all()))
                left, top, right, bottom = button.bbox('button-icon')
                self.assertTrue(0 < left < right < button.winfo_width())
                self.assertTrue(0 < top < bottom < button.winfo_height())

    def test_truncated_caption_reserves_space_for_icon_at_larger_text_size(self):
        root = self.create_root()
        caption = '一个用户创建的很长的项目文件夹名称'
        button = FlatButton(root, caption, icon='folder', anchor='w', max_width=135)
        button.pack()
        set_text_size(root, 'extra_large')
        root.update()

        for icon_side in ('left', 'right'):
            with self.subTest(icon_side=icon_side):
                button.configure(icon_side=icon_side)
                root.update()
                self.assertEqual(button.winfo_reqwidth(), 135)
                self.assertEqual(button.cget('text'), caption)
                text = next(item for item in button.find_all() if button.type(item) == 'text')
                self.assertTrue(button.itemcget(text, 'text').endswith('…'))
                icon_left, _, icon_right, _ = button.bbox('button-icon')
                text_left, _, text_right, _ = button.bbox(text)
                if icon_side == 'left':
                    self.assertLess(icon_right, text_left)
                else:
                    self.assertLess(text_right, icon_left)

        button.place(width=24, height=34)
        root.update()
        self.assertTrue(button.find_withtag('button-icon'))
        self.assertFalse(any(button.type(item) == 'text' for item in button.find_all()))

    def test_icon_tracks_button_foreground_and_disabled_command_state(self):
        for theme in ('light', 'dark', 'cyberpunk'):
            with self.subTest(theme=theme):
                root = self.create_root(theme)
                button = FlatButton(root, command=lambda: 'sent', icon='arrow_up',
                                    variant='primary', width=40, height=40, radius=20)
                button.pack()
                root.update()
                with patch.object(button, 'focus_get', return_value=button):
                    button.draw()
                focus = button.find_all()[-1]
                self.assertEqual(button.itemcget(focus, 'outline'), COLORS['primary_fg'])
                self.assertEqual(button.invoke(), 'sent')
                button.config({'state': 'disabled', 'icon': 'stop'})
                self.assertEqual(button.cget('state'), 'disabled')
                self.assertEqual(button.cget('icon'), 'stop')
                for item in button.find_withtag('button-icon'):
                    self.assertEqual(button.itemcget(item, 'fill'), COLORS['disabled_fg'])
                self.assertIsNone(button.invoke())

    def test_layout_scale_keeps_pixel_dimensions_and_font_points_independent(self):
        for layout_scale in (None, 1.5):
            with self.subTest(layout_scale=layout_scale):
                root = self.create_root(dpi=2.0, layout_scale=layout_scale)
                scale = layout_scale or 1.0
                frame = RoundedFrame(root, padding=(18, 12), radius=12)
                frame.pack()
                button = FlatButton(frame.body, '设置', width=110, height=44, radius=10,
                                    icon='settings', font_size=11, max_width=124)
                button.pack()
                root.update()

                self.assertEqual(display_scale(button), scale)
                self.assertEqual(text_scale(button), scale)
                self.assertEqual(ui_font(button, 11).cget('size'), 11)
                self.assertEqual(button.winfo_reqwidth(), round(110 * scale))
                self.assertEqual(button.winfo_reqheight(), round(44 * scale))
                self.assertEqual(frame.body.pack_info()['padx'], round(18 * scale))
                self.assertEqual(frame.body.pack_info()['pady'], round(12 * scale))
                self.assertEqual(frame.canvas.coords(frame.canvas.find_all()[0])[0], 1 + 12 * scale)
                self.assertEqual(button.coords(button.find_all()[0])[0], 1 + 10 * scale)

                button.configure(width=116, height=48)
                self.assertEqual(button.winfo_reqwidth(), round(116 * scale))
                self.assertEqual(button.winfo_reqheight(), round(48 * scale))
                set_text_size(root, 'extra_large')
                button.configure(text='一个用户创建的很长的项目文件夹名称')
                root.update()
                self.assertEqual(text_scale(button), 1.34 * scale)
                self.assertEqual(ui_font(button, 11).cget('size'), 15)
                self.assertEqual(button.winfo_reqwidth(), round(124 * scale))
