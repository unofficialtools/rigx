import os, strformat

proc main() =
  let who = if paramCount() >= 1: paramStr(1) else: "world"
  echo fmt"Hello, {who}! (from Nim)"

when isMainModule:
  main()
