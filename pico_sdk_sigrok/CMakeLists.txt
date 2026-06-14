# == DO NOT EDIT THE FOLLOWING LINES for the Raspberry Pi Pico VS Code Extension to work ==
if(WIN32)
    set(USERHOME $ENV{USERPROFILE})
else()
    set(USERHOME $ENV{HOME})
endif()
set(sdkVersion 2.1.0)
set(toolchainVersion 13_3_Rel1)
set(picotoolVersion 2.1.0)
set(picoVscode ${USERHOME}/.pico-sdk/cmake/pico-vscode.cmake)
if (EXISTS ${picoVscode})
    include(${picoVscode})
endif()
# ====================================================================================
set(PICO_BOARD pico CACHE STRING "Board type")

cmake_minimum_required(VERSION 3.13)

include(pico_sdk_import.cmake)

project(pico_sdk_sigrok C CXX ASM)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
option(SIGROK_BOARD_EXLINK "Build firmware for the Exlink RP2040 board" OFF)
option(EXLINK_BUILD_JTAG_BRIDGE "Build standalone Exlink RP2040 JTAG bridge" OFF)
#See https://github.com/pico-coder/sigrok-pico/pull/63/  
#setting this allows the DTR to be ignored.  In addition to this, the use of 
#tud_cdc_connected should be replaced with tud_ready.
add_definitions(-DPICO_STDIO_USB_CONNECTION_WITHOUT_DTR=1)
pico_sdk_init()

add_executable(pico_sdk_sigrok
  pico_sdk_sigrok.c
  sr_device.c
)

if(SIGROK_BOARD_EXLINK)
    target_compile_definitions(pico_sdk_sigrok PRIVATE
        SIGROK_BOARD_EXLINK=1
    )
endif()

pico_enable_stdio_usb(pico_sdk_sigrok 1)
pico_enable_stdio_uart(pico_sdk_sigrok 0)

pico_add_extra_outputs(pico_sdk_sigrok)

target_link_libraries(
    pico_sdk_sigrok 
    pico_stdlib
    hardware_adc
    hardware_dma
    hardware_pio
    hardware_sync
#    hardware_sio
#    hardware_gpio
#    hardware_timer
    pico_multicore
    pico_stdio_usb
)

if(SIGROK_BOARD_EXLINK AND EXLINK_BUILD_JTAG_BRIDGE)
    add_executable(exlink_jtag_bridge
        exlink_jtag_bridge/main.c
        exlink_jtag_bridge/jtag_engine.c
        exlink_jtag_bridge/jtag_gpio.c
        exlink_jtag_bridge/jtag_pio.c
        exlink_jtag_bridge/jtag_protocol.c
        exlink_jtag_bridge/usb_cdc_transport.c
    )

    target_compile_definitions(exlink_jtag_bridge PRIVATE
        SIGROK_BOARD_EXLINK=1
        EXLINK_JTAG_DEBUG=0
    )

    target_include_directories(exlink_jtag_bridge PRIVATE
        ${CMAKE_CURRENT_LIST_DIR}
        ${CMAKE_CURRENT_LIST_DIR}/boards
        ${CMAKE_CURRENT_LIST_DIR}/exlink_jtag_bridge
    )

    target_link_libraries(exlink_jtag_bridge
        pico_stdlib
        pico_stdio_usb
        hardware_clocks
        hardware_gpio
        hardware_pio
        hardware_sync
    )

    pico_enable_stdio_usb(exlink_jtag_bridge 1)
    pico_enable_stdio_uart(exlink_jtag_bridge 0)
    pico_add_extra_outputs(exlink_jtag_bridge)
endif()
