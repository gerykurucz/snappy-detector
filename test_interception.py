import interception

ctx = interception.Interception()
print('Devices:', len(ctx.devices))
for d in ctx.devices:
    print(f'  Device {d.hardware_id}: mouse={d.is_mouse}, keyboard={d.is_keyboard}')