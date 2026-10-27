with open('assist.py', 'r') as f:
    lines = f.readlines()

# Fix duplicate comment lines (1117-1118 in 1-indexed = indices 1116-1117 in 0-indexed)
# Line 1118 (index 1117) is the duplicate
if "that need it (e.g., virtual gamepad hold). Default is False." in lines[1117]:
    lines[1117] = ""  # Remove the duplicate

with open('assist.py', 'w') as f:
    f.writelines(lines)
print('Fixed')