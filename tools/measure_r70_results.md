# Measured capability, radius 70 (disc 140x140 px)

Real COCO photographs, human view/stance labels, shipped
engine, circle mode, full sweep per case.

overall: **86%** (344/400)

## by body height in the disc

| height px | detection |
|---|---|
| 140 | 99% (79/80) |
| 90 | 100% (80/80) |
| 50 | 96% (77/80) |
| 24 | 85% (68/80) |
| 12 | 50% (40/80) |

## by visibility

| visibility | detection |
|---|---|
| whole | 99% (79/80) |
| top | 75% (60/80) |
| bottom | 89% (71/80) |
| left | 84% (67/80) |
| right | 84% (67/80) |

## by yaw (view bin)

| yaw bin | detection |
|---|---|
| front | 87% (195/225) |
| back | 81% (101/125) |
| side_left | 96% (24/25) |
| side_right | 96% (24/25) |

## by stance

| stance | detection |
|---|---|
| standing | 84% (147/175) |
| crouch | 87% (87/100) |
| prone | 95% (71/75) |
| jump | 78% (39/50) |

## height x visibility

| height | whole | top | bottom | left | right |
|---|---|---|---|---|---|
| 140 | 100% | 94% | 100% | 100% | 100% |
| 90 | 100% | 100% | 100% | 100% | 100% |
| 50 | 100% | 94% | 100% | 94% | 94% |
| 24 | 100% | 62% | 88% | 88% | 88% |
| 12 | 94% | 25% | 56% | 38% | 38% |

## yaw x visibility (bodies >= 50 px)

| yaw bin | whole | top | bottom | left | right |
|---|---|---|---|---|---|
| front | 100% (27) | 100% (27) | 100% (27) | 96% (27) | 100% (27) |
| back | 100% (15) | 87% (15) | 100% (15) | 100% (15) | 93% (15) |
| side_left | 100% (3) | 100% (3) | 100% (3) | 100% (3) | 100% (3) |
| side_right | 100% (3) | 100% (3) | 100% (3) | 100% (3) | 100% (3) |

## view classification of detected bodies >= 48 px

| label | agreed | detected&classified |
|---|---|---|
| front | 66% | 44 |
| back | 53% | 17 |
| side_left | 40% | 5 |
| side_right | 100% | 3 |

## notes

- diagonal yaw bins (45/135/225/315 deg) have no hand-labelled real photographs in the label set; they are bracketed by the measured cardinals and covered in training by the synthetic 360 deg body model.
- 'detection' = a fused detection inside the visible part of the body; full-sweep still mode = what the live loop converges to over a sweep cycle.
