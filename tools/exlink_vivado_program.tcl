proc exlink_arg {name default_value} {
    global argv
    set count [llength $argv]
    for {set i 0} {$i < $count} {incr i} {
        if {[lindex $argv $i] eq $name && [expr {$i + 1}] < $count} {
            return [lindex $argv [expr {$i + 1}]]
        }
    }
    return $default_value
}

proc exlink_sanitize {text} {
    regsub -all {[\r\n\t ]+} $text {_} text
    return $text
}

set bitstream [exlink_arg "-bitstream" ""]
set xvc_host [exlink_arg "-xvc_host" "localhost"]
set xvc_port [exlink_arg "-xvc_port" "2542"]
set device_index [exlink_arg "-device_index" "0"]
set warmup [exlink_arg "-warmup" "1"]
set runs [exlink_arg "-runs" "3"]

if {$bitstream eq ""} {
    puts "EXLINK_PROGRAM run=0 type=test status=FAIL elapsed_ms=0 reason=missing_bitstream"
    exit 2
}

set exit_code 0
set opened 0

proc exlink_program_once {device bitstream run_index run_type} {
    set elapsed_ms 0
    set reason ""
    set status "PASS"
    if {[catch {
        set_property PROGRAM.FILE $bitstream $device
        set start_ms [clock milliseconds]
        program_hw_devices $device
        set elapsed_ms [expr {[clock milliseconds] - $start_ms}]
        refresh_hw_device $device
    } err]} {
        set status "FAIL"
        set reason [exlink_sanitize $err]
        set elapsed_ms 0
    }
    if {$reason eq ""} {
        puts "EXLINK_PROGRAM run=$run_index type=$run_type status=$status elapsed_ms=$elapsed_ms"
    } else {
        puts "EXLINK_PROGRAM run=$run_index type=$run_type status=$status elapsed_ms=$elapsed_ms reason=$reason"
    }
    flush stdout
    return [expr {$status eq "PASS"}]
}

if {[catch {
    open_hw_manager
    connect_hw_server
    open_hw_target -xvc_url ${xvc_host}:${xvc_port}
    set opened 1
    set devices [get_hw_devices]
    if {[llength $devices] == 0} {
        error "no_hw_devices"
    }
    set programmable {}
    foreach dev $devices {
        if {![catch {get_property PROGRAM.FILE $dev}]} {
            lappend programmable $dev
        }
    }
    if {[llength $programmable] == 0} {
        set programmable $devices
    }
    if {$device_index >= [llength $programmable]} {
        error "device_index_out_of_range"
    }
    set device [lindex $programmable $device_index]

    set run_index 0
    for {set i 0} {$i < $warmup} {incr i} {
        if {![exlink_program_once $device $bitstream $run_index "warmup"]} {
            set exit_code 3
        }
        incr run_index
    }
    for {set i 0} {$i < $runs} {incr i} {
        if {![exlink_program_once $device $bitstream $run_index "test"]} {
            set exit_code 4
        }
        incr run_index
    }
} err]} {
    puts "EXLINK_PROGRAM run=0 type=test status=FAIL elapsed_ms=0 reason=[exlink_sanitize $err]"
    set exit_code 1
}

if {$opened} {
    catch {close_hw_target}
}
catch {disconnect_hw_server}
catch {close_hw_manager}
exit $exit_code
