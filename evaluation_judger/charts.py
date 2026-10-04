"""Report figures from locked score facts, without model-created numbers."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from .judge import write_json
from .rubric import LABELS, METRICS
from .scoring import display, number


COLORS = ('#264B75', '#7295B8', '#B7804A')


def build_charts(facts: dict, report_dir: Path) -> list[dict]:
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    plt.rcParams.update({'font.sans-serif': ['Microsoft YaHei', 'Noto Sans CJK SC', 'SimHei', 'DejaVu Sans'],
                         'axes.unicode_minus': False, 'font.size': 9,
                         'axes.spines.top': False, 'axes.spines.right': False})
    folder = report_dir / 'charts'
    folder.mkdir(parents=True, exist_ok=True)
    subjects = list(facts['subjects'].items())
    figures = []

    def grouped(name, title, labels, values, caption):
        fig, ax = plt.subplots(figsize=(7.1, max(2.8, len(labels)*.64 + .7)))
        width = .72 / len(subjects)
        for index, (sid, subject) in enumerate(subjects):
            ys = [i - .36 + width/2 + index*width for i in range(len(labels))]
            data = [float(number(value)) for value in values[sid]]
            bars = ax.barh(ys, data, height=width*.88, color=COLORS[index], label=subject['name'])
            for bar, value in zip(bars, values[sid]):
                ax.text(float(number(value))+.045, bar.get_y()+bar.get_height()/2, display(value), va='center', fontsize=9)
        ax.set_yticks(range(len(labels)), labels)
        ax.invert_yaxis()
        ax.set_xlim(0, 5.55)
        ax.set_xticks(range(6))
        ax.set_xlabel('质量分（0–5 分）')
        ax.grid(axis='x', alpha=.18)
        ax.set_axisbelow(True)
        ax.legend(loc='lower center', bbox_to_anchor=(.5, 1.02), ncol=len(subjects), frameon=False)
        fig.tight_layout()
        path = folder/(name+'.png')
        fig.savefig(path, dpi=220, facecolor='white', bbox_inches='tight')
        plt.close(fig)
        item = {'title': title, 'path': str(path), 'caption': caption, 'labels': labels, 'series': values}
        write_json(folder/(name+'.json'), item)
        figures.append(item)

    grouped('overall-quality', '总体五项质量指标', [LABELS[m] for m in METRICS[1:]],
            {sid: [subject['averages'][m] for m in METRICS[1:]] for sid,subject in subjects},
            f"全部 {facts['task_count']} 题等权汇总；完成率单独统计，不混入质量分。")
    dimensions = list(facts['dimensions'].items())
    grouped('dimension-quality', '各维度质量均分', [dim for dim,_ in dimensions],
            {sid: [summary['subjects'][sid]['scores']['quality_mean'] for _,summary in dimensions] for sid,_ in subjects},
            '各维度内部任务等权；题数：' + '、'.join(f"{dim} {len(summary['tasks'])}题" for dim,summary in dimensions) + '。数据集总体分直接平均任务，不平均维度均分。')

    if len(subjects) > 1:
        rows = [row for _,summary in dimensions for row in summary['task_results']]
        baseline = subjects[0][0]
        values = {sid: [str(number(row['subjects'][sid]['scores']['quality_mean']) - number(row['subjects'][baseline]['scores']['quality_mean'])) for row in rows] for sid,_ in subjects[1:]}
        chunks = [rows[i:i+15] for i in range(0, len(rows), 15)]
        for chunk_index, chunk in enumerate(chunks):
            offset = chunk_index * 15
            chunk_values = {sid: data[offset:offset+len(chunk)] for sid, data in values.items()}
            fig, ax = plt.subplots(figsize=(7.1, max(3.2,len(chunk)*.34+1)))
            width = .7 / (len(subjects)-1)
            for index,(sid,subject) in enumerate(subjects[1:],1):
                ys = [i-.35+width/2+(index-1)*width for i in range(len(chunk))]
                data = [float(number(value)) for value in chunk_values[sid]]
                bars = ax.barh(ys,data,height=width*.85,color=COLORS[index],label=subject['name']+' − '+subjects[0][1]['name'])
                for bar,value in zip(bars,chunk_values[sid]):
                    x = float(number(value))
                    ax.text(x+(.035 if x>=0 else -.035),bar.get_y()+bar.get_height()/2,display(value),va='center',ha='left' if x>=0 else 'right',fontsize=9)
            ax.axvline(0,color='#666666',linewidth=.9)
            ax.set_yticks(range(len(chunk)),[row['task_id'] for row in chunk])
            ax.invert_yaxis()
            extent=max([abs(float(number(value))) for data in values.values() for value in data]+[.1])+.4
            ax.set_xlim(-extent,extent)
            ax.set_xlabel('质量均分差值（分）；正值表示后列对象得分更高')
            ax.grid(axis='x',alpha=.18)
            ax.set_axisbelow(True)
            ax.legend(loc='lower center',bbox_to_anchor=(.5,1.02),frameon=False,fontsize=9)
            fig.tight_layout()
            suffix = '' if len(chunks)==1 else '-'+str(chunk_index+1)
            path=folder/('task-quality-difference'+suffix+'.png')
            fig.savefig(path,dpi=220,facecolor='white',bbox_inches='tight')
            plt.close(fig)
            title='逐题质量均分差异'+('' if len(chunks)==1 else f' 第 {chunk_index+1} 部分')
            item={'title':title,'path':str(path),'caption':'差值按未舍入分数计算；题号对应下文逐题分数明细。','labels':[row['task_id'] for row in chunk],'series':chunk_values,'baseline':baseline}
            write_json(folder/('task-quality-difference'+suffix+'.json'),item)
            figures.append(item)
        if len(chunks)>1:
            write_json(folder/'task-quality-difference.json',{'title':'逐题质量均分差异','labels':[row['task_id'] for row in rows], 'series':values,'baseline':baseline,'parts':[str(folder/('task-quality-difference-'+str(i+1)+'.json')) for i in range(len(chunks))]})
    return figures
