import re

RULES = [('Modelo Conjugado Dirichlet[-– ]Multinomial', 'Estimativa baseada no histórico'), ('Dirichlet[-– ]Multinomial', 'Estimativa baseada no histórico'), ('Intervalo de Credibilidade(?: Beta)?(?:\\s*\\(IC 90%\\)|\\s*\\(90%\\)|\\s*(?:de )?90%)?', 'Faixa provável de variação (90% de confiança)'), ('IC 90% Beta', 'Faixa provável de variação (90% de confiança)'), ('Empirical Bayes', 'estimativa baseada no histórico'), ('Bayesian[oa]s?', 'baseada no histórico'), ('prior beta', 'histórico de referência'), ('shrinkage', 'ajuste pela amostra')]

def business_text(value):
    if isinstance(value, str):
        for pattern, replacement in RULES:
            value = re.sub(pattern, replacement, value, flags=re.I)
    elif isinstance(value, list):
        return [business_text(item) for item in value]
    elif isinstance(value, dict):
        return {key: business_text(item) for key, item in value.items()}
    return value
