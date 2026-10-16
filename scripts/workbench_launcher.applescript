on run
    my openWorkbench()
end run

on reopen
    my openWorkbench()
end reopen

on openWorkbench()
    try
        set bundlePath to POSIX path of (path to me)
        set settingsPath to bundlePath & "Contents/Resources/Project.plist"
        set projectDirectory to do shell script "/usr/libexec/PlistBuddy -c 'Print :ProjectPath' " & quoted form of settingsPath
        set launcher to projectDirectory & "/run.sh"
        set configPath to projectDirectory & "/config.toml"
        do shell script "/bin/test -x " & quoted form of launcher & " && /bin/test -x " & quoted form of (projectDirectory & "/.venv/bin/python")
        with timeout of 60 seconds
            do shell script quoted form of launcher & " --config " & quoted form of configPath & " workbench --open"
        end timeout
    on error errorMessage number errorNumber
        activate
        display alert "听记暂时无法打开" message ("请确认转写项目仍在原位置。\n\n" & errorMessage & "\n\n如移动了项目，可在新位置重新运行 scripts/build_workbench_app.py 更新应用入口。") as warning
    end try
end openWorkbench
