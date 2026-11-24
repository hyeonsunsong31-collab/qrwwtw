# {{TITLE}} -- Game Boy Advance build (arm-none-eabi-gcc)
CC      = arm-none-eabi-gcc
OBJCOPY = arm-none-eabi-objcopy
CFLAGS  = -mthumb -mthumb-interwork -mcpu=arm7tdmi -O2 -Wall -ffreestanding -fno-builtin
LDFLAGS = -mthumb -mthumb-interwork -nostartfiles -T gba.ld -Wl,--gc-sections

game.gba: game.elf
	$(OBJCOPY) -O binary $< $@
	python -m dev.cli gba-fix $@ --title "{{TITLE}}" --code AJVE

game.elf: crt0.s main.c gba.ld
	$(CC) $(CFLAGS) $(LDFLAGS) crt0.s main.c -o $@

clean:
	rm -f game.elf game.gba
