on run
    try
        activate
        set recordings to choose file with prompt "选择需要转写的录音（可按住 ⌘ 多选）" with multiple selections allowed
        my startTranscription(recordings)
    on error errorMessage number errorNumber
        if errorNumber is not -128 then
            display alert "无法开始手动转写" message errorMessage as warning
        end if
    end try
end run

on open recordings
    try
        my startTranscription(recordings)
    on error errorMessage number errorNumber
        if errorNumber is not -128 then
            display alert "无法开始手动转写" message errorMessage as warning
        end if
    end try
end open

on startTranscription(recordings)
    set projectDirectory to do shell script "/usr/bin/dirname $(/usr/bin/dirname " & quoted form of (POSIX path of (path to me)) & ")"
    set launcher to projectDirectory & "/developer/run.sh"
    set pythonExecutable to projectDirectory & "/.venv/bin/python"
    try
        do shell script "/bin/test -x " & quoted form of launcher & " && /bin/test -x " & quoted form of pythonExecutable
    on error
        error "请将「手动转写.app」放在转写项目文件夹里，并先完成项目安装。"
    end try
    set commandText to "/usr/bin/nohup " & quoted form of launcher & " transcribe --gui --reveal --"
    repeat with recording in recordings
        set commandText to commandText & " " & quoted form of (POSIX path of recording)
    end repeat
    set logDirectory to projectDirectory & "/state/logs"
    do shell script "/bin/mkdir -p " & quoted form of logDirectory
    do shell script commandText & " >> " & quoted form of (logDirectory & "/manual-launch.log") & " 2>&1 < /dev/null &"
end startTranscription
