import interception

ctx = interception.Interception()
print('Devices:', len(ctx.devices))
for d in ctx.devices:
    print(f'  Device: {dir(d)}')