@ {{TITLE}} -- start-up code: the cartridge header, then main()
    .section .crt0, "ax"
    .global _start
    .arm
_start:
    b rom_start
    .fill 156, 1, 0          @ logo area (filled by real cartridges; emulators run without it)
    .fill 12, 1, 0           @ title    (set by jarvis-tool gba-fix)
    .fill 4, 1, 0            @ game code
    .byte 0x30, 0x31         @ maker
    .byte 0x96               @ fixed value
    .byte 0x00, 0x00         @ unit code, device type
    .fill 7, 1, 0
    .byte 0x00               @ version
    .byte 0x00               @ header checksum (set by jarvis-tool gba-fix)
    .byte 0x00, 0x00
rom_start:
    mov r0, #0x12            @ IRQ mode stack
    msr cpsr, r0
    ldr sp, =0x03007FA0
    mov r0, #0x1F            @ system mode stack
    msr cpsr, r0
    ldr sp, =0x03007F00
    ldr r0, =__data_lma      @ copy .data
    ldr r1, =__data_start
    ldr r2, =__data_end
1:  cmp r1, r2
    ldrlt r3, [r0], #4
    strlt r3, [r1], #4
    blt 1b
    ldr r1, =__bss_start     @ clear .bss
    ldr r2, =__bss_end
    mov r3, #0
2:  cmp r1, r2
    strlt r3, [r1], #4
    blt 2b
    ldr r0, =main
    bx r0
