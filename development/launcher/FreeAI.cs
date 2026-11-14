using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Reflection;
using System.Windows.Forms;
[assembly: AssemblyCompany("Free AI")]
[assembly: AssemblyProduct("Free AI")]
[assembly: AssemblyVersion("2.4.2.0")]
[assembly: AssemblyFileVersion("2.4.2.0")]
#if TRAINER
[assembly: AssemblyTitle("Free AI Trainer")]
#else
[assembly: AssemblyTitle("Free AI")]
#endif
internal static class FreeAILauncher {
    private static string Quote(string value) {
        StringBuilder result = new StringBuilder("\""); int slashes = 0;
        foreach(char character in value) {
            if(character == '\\') { slashes++; continue; }
            result.Append('\\', character == '"' ? slashes * 2 + 1 : slashes);
            result.Append(character); slashes = 0;
        }
        result.Append('\\', slashes * 2); result.Append('"'); return result.ToString();
    }
    [STAThread] private static int Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        bool selfTest = Array.IndexOf(args, "--self-test") >= 0;
#if TRAINER
        string title = "Free AI 训练端"; string logName = "trainer-launcher-error.log";
#else
        string title = "Free AI"; string logName = "launcher-error.log";
#endif
        try {
            string python = Path.Combine(root, "runtime", "python", "pythonw.exe");
            string script = Path.Combine(root, "free_ai_bootstrap.py");
            if(!File.Exists(python) || !File.Exists(script)) throw new FileNotFoundException("应用文件不完整，请重新运行安装程序。个人记录会保留。");
            StringBuilder arguments = new StringBuilder("-E -s -B -X utf8 "); arguments.Append(Quote(script));
#if TRAINER
            arguments.Append(" --trainer");
#endif
            foreach(string argument in args) arguments.Append(' ').Append(Quote(argument));
            ProcessStartInfo start = new ProcessStartInfo(python, arguments.ToString());
            start.WorkingDirectory=root; start.UseShellExecute=false; start.CreateNoWindow=true;
            start.WindowStyle=ProcessWindowStyle.Hidden; start.RedirectStandardError=true; start.RedirectStandardOutput=true;
            StringBuilder output = new StringBuilder();
            using(Process process = new Process()) {
                process.StartInfo=start;
                DataReceivedEventHandler capture = delegate(object sender, DataReceivedEventArgs e) { if(e.Data!=null) lock(output) { if(output.Length<65536) output.AppendLine(e.Data); } };
                process.ErrorDataReceived+=capture; process.OutputDataReceived+=capture;
                process.Start(); process.BeginErrorReadLine(); process.BeginOutputReadLine(); process.WaitForExit();
                if(process.ExitCode!=0) throw new InvalidOperationException("程序退出码 " + process.ExitCode + "\r\n" + output);
            }
            return 0;
        } catch(Exception exception) {
            string log = Path.Combine(root, logName);
            try { File.WriteAllText(log, DateTimeOffset.Now.ToString("o") + "\r\n" + exception, Encoding.UTF8); } catch { }
            if(!selfTest) MessageBox.Show(title + " 暂时无法打开。\r\n" + exception.Message.Split('\n')[0] + "\r\n错误记录：" + log, title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
