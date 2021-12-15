import matplotlib.pyplot as plt
import matplotlib.animation as animation
from matplotlib.lines import Line2D
import time
import sys


def read_scores(week):
    with open('week_{}_scores.csv'.format(week), 'r') as csv:
        lines = csv.readlines()
    data = {}
    fields = []
    for field in lines[0].split(','):
        field = field.strip()
        data.update({field: []})
        fields.append(field)

    for line in lines[1:]:
        split_line = line.split(',')
        for i in range(len(split_line)):
            data[fields[i]].append(split_line[i])
    return data


class Graph:
    def __init__(self, ax, ax2, week, team_1_input, team_2_input):
        self.ax = ax
        self.right_ax = ax2
        self.week_num = week
        self.team_1_input = team_1_input
        self.team_2_input = team_2_input
        self.x_data = [0]
        self.y_data_team_1 = [0]
        self.y_data_team_2 = [0]
        self.y_data_right = [0]

        self.all_data = read_scores(self.week_num)

        self.all_data_team_1, self.team_1_name = self.get_array(self.team_1_input, 'PTS')
        self.all_data_team_2, self.team_2_name = self.get_array(self.team_2_input, 'PTS')
        self.winner_input = self.get_winner()
        self.all_data_win_pct, self.winner_team_name = self.get_array(self.winner_input, 'WPCT')

        self.num_points = len(self.all_data_team_1)
        self.all_x_data = list(range(self.num_points))
        self.right_ax.set_ylim(0, 1)
        self.ax.set_xlim(0, self.num_points)
        self.prev_point = -1

        if self.winner_input == self.team_1_input:
            self.line_team_1 = Line2D(self.x_data, self.y_data_team_1, color='green')
            self.line_team_2 = Line2D(self.x_data, self.y_data_team_2, color='red', alpha=0.5)
            self.ax.set_ylim(0, float(self.all_data_team_1[-1]) + 10)
            self.ax.set_title('{} beats {} {:.1f} - {:.1f}'.format(self.team_1_name, self.team_2_name,
                                                                   float(self.all_data_team_1[-1]),
                                                                   float(self.all_data_team_2[-1])))
        else:
            self.line_team_1 = Line2D(self.x_data, self.y_data_team_1, color='red', alpha=0.5)
            self.line_team_2 = Line2D(self.x_data, self.y_data_team_2, color='green')
            self.ax.set_ylim(0, float(self.all_data_team_2[-1]) + 10)
            self.ax.set_title('{} beats {} {:.1f} - {:.1f}'.format(self.team_2_name, self.team_1_name,
                                                                   float(self.all_data_team_2[-1]),
                                                                   float(self.all_data_team_1[-1])))

        self.line_right = Line2D(self.x_data, self.y_data_right, color='blue', alpha=0.3, dashes=(2, 1))
        self.ax.add_line(self.line_team_1)
        self.ax.add_line(self.line_team_2)
        self.right_ax.add_line(self.line_right)
        self.ax.set_ylabel('Points')
        self.right_ax.set_ylabel('Win Probability')
        self.ax.set_xticks([])
        self.right_ax.set_yticks([0, 0.25, 0.5, 0.75, 1])

    def get_array(self, name, col):
        name = name.upper()
        data = []
        formatted_name = ''

        for key in self.all_data.keys():
            search_key = key.upper()
            if name in search_key and col in search_key:
                data = self.all_data[key]
                split_name = key.split()
                formatted_name = ' '.join(split_name[:-1])

        if not data or not formatted_name:
            return None
        return data, formatted_name

    def get_winner(self):
        team_1_final = float(self.all_data_team_1[-1])
        team_2_final = float(self.all_data_team_2[-1])
        if team_1_final > team_2_final:
            return self.team_1_input
        else:
            return self.team_2_input

    def update(self, i):
        if i >= self.num_points or i < self.prev_point:
            time.sleep(5)
            self.x_data = [0]
            self.y_data_team_1 = [0]
            self.y_data_team_2 = [0]
            self.y_data_right = [0]
        self.prev_point = i

        self.x_data.append(self.all_x_data[i])
        self.y_data_team_1.append(float(self.all_data_team_1[i]))
        self.y_data_team_2.append(float(self.all_data_team_2[i]))
        self.y_data_right.append(float(self.all_data_win_pct[i]))

        self.line_team_1.set_data(self.x_data, self.y_data_team_1)
        self.line_team_2.set_data(self.x_data, self.y_data_team_2)
        self.line_right.set_data(self.x_data, self.y_data_right)

        return self.line_team_1, self.line_team_2, self.line_right


def main(week, save=False):
    team_1_input = input('Team 1: ')
    team_2_input = input('Team 2: ')
    fig, ax = plt.subplots()
    ax2 = ax.twinx()
    graph = Graph(ax, ax2, week, team_1_input, team_2_input)
    ani = animation.FuncAnimation(fig, graph.update, graph.num_points, interval=8)
    if save:
        ani.save('movie.gif')
    plt.show()


if __name__ == '__main__':
    argv = sys.argv
    save_flag = False
    if len(argv) >= 2:
        week_number = str(argv[1])
        if '-s' in argv:
            save_flag = True
    else:
        week_number = input('Week Number: ')
    main(week_number, save_flag)


# ani.save('movie.mov')
# writer = animation.FFMpegWriter(fps=15, metadata=dict(artist='Me'), bitrate=1800)
# ani.save('movie1.gif', writer=writer)
# gif_writer = animation.PillowWriter(fps=200)
# ani.save('movie.gif', writer=gif_writer)
