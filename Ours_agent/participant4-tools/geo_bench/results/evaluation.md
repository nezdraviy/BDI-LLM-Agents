# Результаты автоматической оценки (3D-модель)

## Подробно, seed 0

| scenario | status | injected | detected | recall | false_alarms | alert_sent | first_detection | actions | retries | replans | passed |
|---|---|---|---|---|---|---|---|---|---|---|---|
| clean | succeeded | 0 | 0 | — | 0 | ❌ | — | 10 | 0 | 0 | ✅ |
| porosity_spike | succeeded | 12 | 12 | 1.0 | 0 | ✅ | check_poro_perm (действие №5) | 10 | 0 | 0 | ✅ |
| perm_mismatch | succeeded | 12 | 12 | 1.0 | 0 | ✅ | check_poro_perm (действие №5) | 10 | 0 | 0 | ✅ |
| oil_below_owc | succeeded | 8 | 8 | 1.0 | 0 | ✅ | check_contacts (действие №6) | 10 | 0 | 0 | ✅ |
| porous_shale | succeeded | 10 | 10 | 1.0 | 0 | ✅ | check_facies (действие №4) | 10 | 0 | 0 | ✅ |
| well_mismatch | succeeded | 8 | 8 | 1.0 | 0 | ✅ | check_poro_perm (действие №5) | 10 | 0 | 0 | ✅ |
| range_violation | succeeded | 6 | 6 | 1.0 | 0 | ✅ | check_ranges (действие №3) | 10 | 0 | 0 | ✅ |
| percent_units | succeeded | 4000 | 4000 | 1.0 | 0 | ✅ | check_ranges (действие №3) | 5 | 0 | 1 | ✅ |
| combined | succeeded | 32 | 32 | 1.0 | 0 | ✅ | check_poro_perm (действие №5) | 10 | 0 | 0 | ✅ |
| flaky_storage | succeeded | 0 | 0 | — | 0 | ❌ | — | 12 | 2 | 0 | ✅ |

**Пройдено сценариев: 10/10**

## Сводка по 30 моделям

| scenario | runs | passed | mean recall | false alarms (sum) |
|---|---|---|---|---|
| clean | 30 | 30 | — | 0 |
| porosity_spike | 30 | 30 | 1.000 | 0 |
| perm_mismatch | 30 | 30 | 1.000 | 0 |
| oil_below_owc | 30 | 30 | 1.000 | 0 |
| porous_shale | 30 | 30 | 1.000 | 0 |
| well_mismatch | 30 | 30 | 1.000 | 0 |
| range_violation | 30 | 30 | 1.000 | 0 |
| percent_units | 30 | 30 | 1.000 | 0 |
| combined | 30 | 30 | 1.000 | 0 |
| flaky_storage | 30 | 30 | — | 0 |
