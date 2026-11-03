"""Original, editable publication schematics at the paper's 5.5-inch text width.

Follows ChenLiu-1996/figures4papers scientific-figure-making (b709de17):
sans-serif typography, restrained blue/green/red palette, minimal ink, PDF/SVG
and 300 dpi PNG. Figure 1 describes services, not a mandatory method sequence.
Figure 2 describes only the common Native profile's staged-source transition.
"""
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Polygon
from matplotlib.path import Path as MPath

ROOT = Path(__file__).resolve().parents[1]
C = dict(ink='#223348', muted='#596B7C', blue='#0F4D92', blue2='#3775BA',
         blue_bg='#EDF3FA', green='#38735D', green_bg='#EDF6F0',
         red='#B64342', red_bg='#FAF0EE', line='#BECAD5',
         light='#F7F9FB', violet='#77618D', violet_bg='#F4F0F8')


def apply_publication_style():
    plt.rcParams.update({'font.family': ['Arial', 'Helvetica', 'DejaVu Sans'],
                         'font.size': 8.6, 'text.color': C['ink'],
                         'axes.spines.top': False, 'axes.spines.right': False,
                         'legend.frameon': False, 'svg.fonttype': 'none',
                         'pdf.fonttype': 42, 'savefig.facecolor': 'white',
                         'mathtext.fontset': 'stix'})


def canvas(h):
    fig = plt.figure(figsize=(5.5, h))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set(xlim=(0, 5.5), ylim=(0, h))
    ax.axis('off')
    return fig, ax


def text(ax, x, y, s, size=8.5, color=None, weight='normal', ha='center', **kw):
    return ax.text(x, y, s, fontsize=size, color=color or C['ink'], weight=weight,
                   ha=ha, va='center', linespacing=1.25, **kw)


def rect(ax, x, y, w, h, fill='white', edge=None, radius=.045, lw=.65, **kw):
    patch = FancyBboxPatch((x,y),w,h,boxstyle=f'round,pad=0,rounding_size={radius}',
                           facecolor=fill,edgecolor=edge or C['line'],linewidth=lw,**kw)
    ax.add_patch(patch)
    return patch


def card(ax, x, y, w, h, title, body='', accent=None, fill='white', title_size=8.8):
    rect(ax,x,y,w,h,fill=fill)
    if accent:
        ax.plot([x+.015,x+.015],[y+.08,y+h-.08],color=accent,lw=2.0,solid_capstyle='round')
    text(ax,x+w/2,y+h*.68,title,size=title_size,weight='bold',color=accent or C['ink'])
    if body:
        text(ax,x+w/2,y+h*.28,body,size=7.8,color=C['muted'])


def arrow(ax, points, color=None, dashed=False, lw=.9):
    path=MPath(points,[MPath.MOVETO]+[MPath.LINETO]*(len(points)-1))
    ax.add_patch(FancyArrowPatch(path=path,arrowstyle='-|>',mutation_scale=7.5,
                                linewidth=lw,color=color or C['muted'],
                                linestyle=(0,(2.4,2.3)) if dashed else '-',
                                capstyle='round',joinstyle='round'))


def panel_label(ax,x,y,letter,title,color):
    text(ax,x,y,letter,size=8.5,weight='bold',color=color,ha='left')
    text(ax,x+.19,y,title,size=8.9,weight='bold',ha='left')


def finalize_figure(fig, name):
    fig.canvas.draw()
    renderer=fig.canvas.get_renderer()
    w,h=fig.canvas.get_width_height()
    for ax in fig.axes:
        for t in ax.texts:
            box=t.get_window_extent(renderer)
            assert box.x0>=-1 and box.y0>=-1 and box.x1<=w+1 and box.y1<=h+1, t.get_text()
    for fmt in ['pdf','svg','png']:
        fig.savefig(ROOT/'figures'/f'{name}.{fmt}',dpi=300,facecolor='white')
    plt.close(fig)


def architecture():
    fig,ax=canvas(3.84)
    # Recipe is a thin source of configuration, visually separate from execution.
    rect(ax,.14,3.27,5.22,.44,fill=C['light'],edge=C['line'])
    text(ax,.29,3.55,'RESOLVED RECIPE',size=8.0,weight='bold',color=C['blue'],ha='left')
    text(ax,.29,3.38,'method  /  stages  /  role bindings  /  tasks  /  budgets  /  source locks',size=8.1,ha='left')
    rect(ax,.14,2.30,2.62,.76,fill=C['blue_bg'],edge='#D8E3EF')
    rect(ax,2.95,2.30,2.41,.76,fill=C['light'],edge='#E0E6ED')
    panel_label(ax,.29,2.86,'a','Method-defined control',C['blue'])
    text(ax,.29,2.62,'typed stages + search / selection',size=8.2,ha='left')
    text(ax,.29,2.43,'proposal   reflection   self-evidence*',size=7.7,color=C['muted'],ha='left')
    panel_label(ax,3.10,2.86,'b','Independent role bindings',C['blue'])
    text(ax,3.10,2.62,'optimizer A / subject B / grader C',size=8.0,ha='left')
    text(ax,3.10,2.43,'compatible runtimes; grader optional',size=7.7,color=C['muted'],ha='left')
    arrow(ax,[(1.44,3.27),(1.44,3.07)],dashed=True,color=C['blue2'],lw=.7)
    arrow(ax,[(4.15,3.27),(4.15,3.07)],dashed=True,color=C['blue2'],lw=.7)
    # Main proposal -> artifact -> execution path. A stacked sheet denotes identity.
    card(ax,.25,1.52,1.35,.55,'Optimizer','propose / edit',accent=C['blue'])
    for dx,dy in [(-.06,-.06),(-.03,-.03)]:
        rect(ax,2.12+dx,1.52+dy,1.32,.55,fill='#F7F4FA',edge='#D6CCDF')
    card(ax,2.12,1.52,1.32,.55,'Candidate','immutable artifact',accent=C['violet'],fill=C['violet_bg'])
    card(ax,3.96,1.52,1.29,.55,'Subject','execute on tasks',accent=C['blue'])
    arrow(ax,[(.93,2.30),(.93,2.08)],color=C['blue2'])
    arrow(ax,[(4.61,2.30),(4.61,2.08)],dashed=True,color=C['blue2'],lw=.7)
    arrow(ax,[(1.61,1.80),(2.03,1.80)],color=C['blue2'])
    text(ax,1.81,1.99,'propose',size=7.4,color=C['muted'])
    arrow(ax,[(3.45,1.80),(3.94,1.80)],color=C['blue2'])
    text(ax,3.70,1.99,'materialize',size=7.2,color=C['muted'])
    # Evidence returns through one visible policy boundary, without crossing arrows.
    card(ax,3.96,.71,1.29,.54,'Evaluation','rule / private grader',accent=C['green'],fill=C['green_bg'],title_size=8.5)
    card(ax,1.56,.71,1.88,.54,'Evidence policy','visibility + selection eligibility',accent=C['green'],fill=C['green_bg'])
    arrow(ax,[(4.61,1.51),(4.61,1.27)],color=C['muted'])
    text(ax,4.24,1.40,'prediction',size=7.3,color=C['muted'])
    arrow(ax,[(3.94,.98),(3.46,.98)],color=C['green'])
    text(ax,3.69,1.14,'grade',size=7.3,color=C['green'])
    arrow(ax,[(1.54,.98),(.93,.98),(.93,1.50)],color=C['green'])
    text(ax,.31,1.25,'released\nevidence',size=7.6,color=C['green'],ha='left')
    text(ax,2.75,.53,'* Self-evidence is consumed only when the method invokes it.',size=7.4,color=C['muted'])
    # An unobtrusive foundation band represents shared records, not another algorithm.
    ax.plot([.14,5.36],[.39,.39],color=C['line'],lw=.7)
    text(ax,.22,.24,'SHARED SERVICES',size=7.7,weight='bold',color=C['blue'],ha='left')
    text(ax,1.67,.24,'lineage / receipts / accounting / recovery / exports',size=7.7,color=C['muted'],ha='left')
    finalize_figure(fig,'architecture')


def native_transition():
    fig,ax=canvas(4.02)
    panel_label(ax,.14,3.86,'a','Execute with stable active state',C['blue'])
    card(ax,.14,3.11,1.56,.49,r'Active $A_t$','frozen executor',accent=C['blue'],fill=C['blue_bg'])
    card(ax,1.98,3.11,1.56,.49,r'Working $W_t$','editable state',accent=C['violet'],fill=C['violet_bg'])
    card(ax,3.82,3.11,1.54,.49,r'Goal + evidence','permitted inputs',accent=C['green'],fill=C['green_bg'])
    rect(ax,.14,2.23,5.22,.65,fill=C['blue_bg'],edge='#D8E3EF')
    text(ax,2.75,2.71,r'Every phase runs $A_t$; edits accumulate in the working state.',size=8.0,color=C['blue'])
    centers=[.80,2.10,3.40,4.70]
    for i,(x,label) in enumerate(zip(centers,['Observe','Propose','Act','Reflect'])):
        text(ax,x,2.42,label,size=8.8,weight='bold',color=C['blue'])
        if i<3:
            arrow(ax,[(x+.39,2.42),(centers[i+1]-.42,2.42)],color=C['blue2'],lw=.75)
    for x in [.92,2.76,4.59]:
        arrow(ax,[(x,3.10),(x,2.90)],color=C['line'],lw=.8)
    arrow(ax,[(2.75,2.22),(2.75,1.99)],color=C['violet'])
    text(ax,2.75,1.85,r'Generated candidate $\widetilde{W}_{t+1}$',size=9.0,weight='bold',color=C['violet'])
    panel_label(ax,.14,1.57,'b','Check viability',C['green'])
    # Compact diamond; orthogonal branches make the two outcomes immediately visible.
    diamond=Polygon([(2.75,1.57),(3.20,1.25),(2.75,.93),(2.30,1.25)],
                    closed=True,facecolor=C['green_bg'],edgecolor='#77A58E',linewidth=.85)
    ax.add_patch(diamond)
    text(ax,2.75,1.25,'Viable?',size=8.6,weight='bold',color=C['green'])
    arrow(ax,[(2.75,1.72),(2.75,1.60)],color=C['violet'])
    arrow(ax,[(2.29,1.25),(1.14,1.25),(1.14,.95)],color=C['red'])
    arrow(ax,[(3.21,1.25),(4.36,1.25),(4.36,.95)],color=C['green'])
    text(ax,1.53,1.39,'invalid',size=7.8,color=C['red'])
    text(ax,3.94,1.39,'valid',size=7.8,color=C['green'])
    for x,color,fill,title,equation,detail in [
        (.14,C['red'],C['red_bg'],'RETAIN + REPAIR',r'$A_{t+1}=A_t$','Keep the exact failed edits.'),
        (3.36,C['green'],C['green_bg'],'ACTIVATE',r'$A_{t+1}=\widetilde{W}_{t+1}$','No score-increase gate.')]:
        rect(ax,x,.24,2.00,.69,fill=fill,edge='#DCC8C6' if x<1 else '#BED9CA')
        text(ax,x+1,.78,title,size=7.7,weight='bold',color=color)
        text(ax,x+1,.55,equation,size=11.2,color=color)
        text(ax,x+1,.35,detail,size=7.5,color=C['muted'])
    text(ax,2.75,.08,r'Both outcomes retain the working candidate: $W_{t+1}=\widetilde{W}_{t+1}$.',size=7.9,color=C['muted'])
    finalize_figure(fig,'native-transition')


if __name__=='__main__':
    apply_publication_style()
    architecture()
    native_transition()
    print('Wrote two publication figures: PDF / editable SVG / 300-dpi PNG.')
