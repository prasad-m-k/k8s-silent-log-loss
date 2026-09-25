import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
plt.rcParams["mathtext.fontset"] = "stix"
plt.rcParams["font.family"] = "STIXGeneral"
EQ = {
 "eq1": r"$R_m \;=\; \dfrac{L_{\mathrm{lost}}}{L_{\mathrm{total}}}$",
 "eq2": r"$S \;\leq\; s_{\max} \;<\; S + W\, I_m$",
 "eq3": r"$\lim_{t \to \infty} R_m(t) \;=\; 1 - \dfrac{R}{W}\,, \qquad W > R$",
 "eq4": r"$t^{*} \;\approx\; \dfrac{w + c}{1 - R/W}$",
 "eq5": r"$\hat{B} \;=\; \sum_{i \in F} \max\left(0,\; s_i - o_i\right)$",
 "eq6": r"$0 \;\leq\; \hat{B} - B \;\lesssim\; |A|\, R\, \Delta$",
 "eq7": r"$R_e \;\geq\; W - R$",
 "eq8": r"$N_{\mathrm{drop}} \;\approx\; r\,d - Q - b\,, \qquad d < T_{\mathrm{exp}}$",
}
for k, tex in EQ.items():
    fig = plt.figure(figsize=(0.01, 0.01))
    fig.text(0, 0, tex, fontsize=13)
    fig.savefig(f"eq/{k}.png", dpi=300, bbox_inches="tight", pad_inches=0.03, facecolor="white")
    plt.close(fig)
print("ok")
