# Fatores de impacto ambiental, versionados. Nunca gravar estes números no banco:
# o impacto é sempre calculado na hora, a partir do volume descartado.
IMPACT_FACTORS = {
    "version": "2026-10",
    "water_l_per_l_oluc": 1_000_000,  # regra prática AMBIOLUC/MMA (CONAMA 362/2005)
    "co2e_kg_per_l_oluc": 1.0,  # conservador: rendimento 0,75 x ~1,3 kg CO2e/L de óleo básico
}

IMPACT_SOURCES = {
    "water_l_per_l_oluc": (
        "Regra prática: 1 litro de óleo lubrificante usado pode contaminar até "
        "1 milhão de litros de água (AMBIOLUC/MMA; Resolução CONAMA 362/2005)."
    ),
    "co2e_kg_per_l_oluc": (
        "Estimativa conservadora: rendimento do rerrefino de 0,75 x cerca de 1,3 kg de CO2e "
        "evitado por litro de óleo básico (faixa de 0,9 a 2,0 kg CO2e por litro de óleo usado)."
    ),
}
