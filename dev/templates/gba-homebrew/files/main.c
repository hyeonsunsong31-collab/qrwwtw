/* {{TITLE}} -- draws in bitmap mode 3 and moves a square with the D-pad */
typedef unsigned short u16;
typedef unsigned int u32;
#define REG_DISPCNT (*(volatile u32 *)0x04000000)
#define REG_VCOUNT  (*(volatile u16 *)0x04000006)
#define REG_KEYINPUT (*(volatile u16 *)0x04000130)
#define VRAM ((volatile u16 *)0x06000000)
#define RGB(r, g, b) ((r) | ((g) << 5) | ((b) << 10))
#define KEY_RIGHT 0x0010
#define KEY_LEFT  0x0020
#define KEY_UP    0x0040
#define KEY_DOWN  0x0080

static void rect(int x, int y, int w, int h, u16 c) {
    for (int j = y; j < y + h; j++)
        for (int i = x; i < x + w; i++)
            if (i >= 0 && i < 240 && j >= 0 && j < 160) VRAM[j * 240 + i] = c;
}

int main(void) {
    REG_DISPCNT = 0x0403;               /* mode 3, background 2 on */
    int x = 110, y = 70;
    rect(0, 0, 240, 160, RGB(2, 3, 8));
    while (1) {
        while (REG_VCOUNT >= 160) {}
        while (REG_VCOUNT < 160) {}
        u16 keys = ~REG_KEYINPUT;
        rect(x, y, 20, 20, RGB(2, 3, 8));
        if (keys & KEY_RIGHT) x++;
        if (keys & KEY_LEFT) x--;
        if (keys & KEY_DOWN) y++;
        if (keys & KEY_UP) y--;
        rect(x, y, 20, 20, RGB(7, 23, 31));
    }
    return 0;
}
