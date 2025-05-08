# /bin/bash

# Re-format staged files only

INCLUDE_EXTENSIONS="py"

git diff --name-only --cached | while read -r file; do
  if [ -f "$file" ]; then
    # Get file extension
    extension="${file##*.}"
    
    # Check if extension exactly matches one in INCLUDE_EXTENSIONS
    if echo "$INCLUDE_EXTENSIONS" | tr ',' '\n' | grep -Fx "$extension" > /dev/null; then
      echo "Formatting $file"
      autoflake --in-place --remove-all-unused-imports "$file"
      black "$file"
      isort "$file"
    fi
  fi
done