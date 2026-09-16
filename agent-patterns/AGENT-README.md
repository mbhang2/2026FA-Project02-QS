# Setting Up Opencode Agents (For Students)

Opencode needs to know where to find our project's custom agents, skills, and commands. By default, it looks in a hidden configuration folder on your computer (specifically `~/.config/opencode`).

Instead of copying these files over to that hidden folder manually, we are going to create "shortcuts" (technically called *symbolic links*).

**Why do this?** Because once the shortcut is created, Opencode will look directly into this project folder. Whenever you pull the latest code for the practicum, your Opencode agents will automatically update without you needing to do anything else the next time you reload Opencode.

Follow the step-by-step instructions for your operating system below.

---

## Step 1: Open the Terminal in VS Code

To make sure we are running commands in the correct folder:
1. Open this project folder in **VS Code**.
2. Open the integrated terminal by clicking **Terminal > New Terminal** in the top menu (or use the shortcut `` Ctrl + ` ``).

---

## Step 2: Run the Commands for Your Operating System

### 🍎 Mac and 🐧 Linux

Copy and paste the following block of code into your terminal and press **Enter**:

```bash
# 1. Create the opencode config folder if it doesn't exist yet
mkdir -p ~/.config/opencode

# 2. Create the shortcuts pointing to this project folder
ln -s "$(pwd)/agent-patterns/agents" ~/.config/opencode/agents
ln -s "$(pwd)/agent-patterns/skills" ~/.config/opencode/skills
ln -s "$(pwd)/agent-patterns/commands" ~/.config/opencode/commands

echo "Done! Restart Opencode to use the new agents and skills."
```

*(Note: If you get an error saying "File exists", it means you already have a folder with that name in your config. You will need to delete the old `agents`, `skills`, or `commands` folders inside `~/.config/opencode/` first, and then run the code again.)*

---

### 🪟 Windows (PowerShell)

In your VS Code terminal, make sure the terminal type says **PowerShell** (look at the dropdown menu on the top-right of the terminal panel).

Copy and paste this block of code into the terminal and press **Enter**:

```powershell
# 1. Define where Opencode looks for configs on Windows
$configDir = "$env:USERPROFILE\.config\opencode"

# 2. Create the folder if it doesn't exist yet
New-Item -ItemType Directory -Force -Path $configDir

# 3. Create the shortcuts (Symbolic Links) pointing to this project
New-Item -ItemType SymbolicLink -Path "$configDir\agents" -Target "$PWD\agent-patterns\agents"
New-Item -ItemType SymbolicLink -Path "$configDir\skills" -Target "$PWD\agent-patterns\skills"
New-Item -ItemType SymbolicLink -Path "$configDir\commands" -Target "$PWD\agent-patterns\commands"

Write-Host "Done! Restart Opencode to use the new agents and skills."
```

#### 🚨 Windows Troubleshooting
Windows has strict security rules about creating shortcuts (symbolic links). If you get a red error saying **"Administrator privilege required"** or **"Developer Mode is not enabled"**, follow these fallback steps:

1. Click your Windows Start button and type **PowerShell**.
2. Right-click on "Windows PowerShell" and select **Run as Administrator**. Click "Yes" if it asks for permission.
3. You need to navigate to this project folder. Type `cd ` (with a space at the end) and then type the full path to your project folder. Press Enter.
   *(Example: `cd C:\Users\YourName\Documents\Practicum\2026FA-Project02-2-QS`)*
4. Once you are in the correct folder, copy and paste the PowerShell code block above again.
