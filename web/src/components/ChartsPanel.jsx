import React from 'react';
import ReactECharts from 'echarts-for-react';

export default function ChartsPanel({ isDarkMode }) {
  const option = {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      backgroundColor: isDarkMode ? '#18181b' : '#ffffff',
      borderColor: isDarkMode ? '#27272a' : '#e4e4e7',
      textStyle: { color: isDarkMode ? '#f43f5e' : '#09090b', fontFamily: 'JetBrains Mono' },
    },
    grid: {
      left: '3%',
      right: '4%',
      bottom: '3%',
      top: '12%',
      containLabel: true,
    },
    xAxis: {
      type: 'category',
      data: ['14:00', '14:30', '15:00', '15:30', '16:00', '16:15', '16:30'],
      axisLine: { lineStyle: { color: isDarkMode ? '#3f3f46' : '#d4d4d8' } },
      axisLabel: { color: isDarkMode ? '#a1a1aa' : '#71717a', fontFamily: 'DM Sans' }
    },
    yAxis: {
      type: 'value',
      axisLine: { lineStyle: { color: isDarkMode ? '#3f3f46' : '#d4d4d8' } },
      splitLine: { lineStyle: { color: isDarkMode ? '#27272a' : '#f4f4f5' } },
      axisLabel: {
        color: isDarkMode ? '#a1a1aa' : '#71717a',
        formatter: '{value}%',
        fontFamily: 'JetBrains Mono'
      }
    },
    series: [
      {
        name: '$FIGHT Runner Return',
        type: 'line',
        smooth: true,
        data: [0, 1.8, 3.2, 5.4, 7.8, 9.5, 12.55],
        itemStyle: { color: '#10b981' },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(16, 185, 129, 0.35)' },
              { offset: 1, color: 'rgba(16, 185, 129, 0.0)' }
            ]
          }
        }
      }
    ]
  };

  return (
    <div className="bg-white dark:bg-[#0c0c0f] border border-zinc-200 dark:border-zinc-800 rounded-xl p-5 mb-6 shadow-sm">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h3 className="text-sm font-semibold text-zinc-950 dark:text-zinc-50">Active Trade Velocity & Trailing Stop Window</h3>
          <p className="text-xs text-zinc-500 dark:text-zinc-400">Real-time on-chain multiple tracking vs 10-minute momentum stop</p>
        </div>
        <span className="text-xs font-mono px-2 py-0.5 rounded bg-emerald-100 text-emerald-800 dark:bg-emerald-950/50 dark:text-emerald-400">
          +12.55% PEAK
        </span>
      </div>
      <ReactECharts option={option} style={{ height: '240px', width: '100%' }} />
    </div>
  );
}
