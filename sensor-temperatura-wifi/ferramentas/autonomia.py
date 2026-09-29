#!/usr/bin/env python3
"""Estimativa de autonomia da bateria do sensor.

Os valores padrão são conservadores para o ESP32-C3 com o firmware deste projeto.
Meça o seu protótipo (multímetro em µA no sono e um medidor de energia, como o
Nordic PPK2 ou o Joulescope, nos despertares) e ajuste os parâmetros.

Exemplo:
  python3 autonomia.py --medicao-min 5 --envio-min 60 --bateria-mah 3000
"""

import argparse


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--medicao-min", type=float, default=5, help="intervalo entre medições (min)")
    ap.add_argument("--envio-min", type=float, default=60, help="intervalo entre envios Wi-Fi (min)")
    ap.add_argument("--sono-ua", type=float, default=9, help="corrente total em deep sleep (µA)")
    ap.add_argument("--medicao-mas", type=float, default=5, help="carga por despertar só para medir (mA·s)")
    ap.add_argument("--envio-mas", type=float, default=100, help="carga por envio Wi-Fi (mA·s)")
    ap.add_argument("--bateria-mah", type=float, default=3000, help="capacidade nominal (mAh)")
    ap.add_argument("--aproveitamento", type=float, default=0.8,
                    help="fração útil da capacidade (temperatura, corte de tensão, envelhecimento)")
    ap.add_argument("--autodescarga-ano", type=float, default=0.01, help="autodescarga por ano (fração)")
    a = ap.parse_args()

    dia_s = 86400
    medicoes = dia_s / (a.medicao_min * 60)
    envios = dia_s / (a.envio_min * 60)
    sono = a.sono_ua / 1000 * dia_s
    med = medicoes * a.medicao_mas
    env = envios * a.envio_mas
    total_mah_dia = (sono + med + env) / 3600
    util = a.bateria_mah * a.aproveitamento
    anos = util / (total_mah_dia * 365 + a.bateria_mah * a.autodescarga_ano)

    print(f"Consumo diário: {total_mah_dia:.2f} mAh/dia  ({total_mah_dia * 365:.0f} mAh/ano)")
    for nome, v in (("sono", sono), ("medições", med), ("envios Wi-Fi", env)):
        print(f"  {nome:13s} {v / 3600:6.2f} mAh/dia  ({v / (sono + med + env) * 100:4.1f} %)")
    print(f"Autonomia estimada: {anos:.1f} anos com {a.bateria_mah:.0f} mAh "
          f"({a.aproveitamento * 100:.0f} % aproveitável)")


if __name__ == "__main__":
    main()
