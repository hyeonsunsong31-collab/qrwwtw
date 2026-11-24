OUTPUT_ARCH(arm)
ENTRY(_start)
MEMORY {
  rom  : ORIGIN = 0x08000000, LENGTH = 32M
  iwram : ORIGIN = 0x03000000, LENGTH = 32K
  ewram : ORIGIN = 0x02000000, LENGTH = 256K
}
SECTIONS {
  .text : { KEEP(*(.crt0)) *(.text*) *(.rodata*) . = ALIGN(4); } > rom
  __data_lma = .;
  .data : { __data_start = .; *(.data*) . = ALIGN(4); __data_end = .; } > iwram AT > rom
  .bss (NOLOAD) : { __bss_start = .; *(.bss*) *(COMMON) . = ALIGN(4); __bss_end = .; } > iwram
}
