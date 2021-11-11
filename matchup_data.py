from yahoo_oauth import OAuth2
import time
import datetime
import sys
TEAMS = ['Popcorn', 'Quads', 'Dynasty', 'Doinkers', 'Booze', 'Kickitungs', 'Mustard', 'Sandbags', 'Squirt', 'Sauce',
         'BONEZONE', 'Fútbol']
FULL_TEAMS = ['Popcorn Fingers', "Carrie's Quads", 'Dark Horse Dynasty', 'Doinkers', "I'm here 4 the Booze",
              'Kickitungs', 'Mean Mr. Mustard', 'Sandbags', 'Squirt', 'Szechuan Sauce', 'The BONEZONE',
              'Washington Fútbol Team']
INTERVAL = 30  # seconds
TIMEOUT = 35  # minutes


def parse_data(response):
    if 'error' in response:
        return None
    matchups = response['fantasy_content']['league'][1]['scoreboard']['0']['matchups']
    data = {}
    for i in range(6):
        teams = matchups[str(i)]['matchup']['0']['teams']
        for j in range(2):
            data_list = teams[str(j)]['team']
            team_name = ''
            win_probability = 0.0
            points = 0.0
            proj_points = 0.0
            for item in data_list[0]:
                if 'name' in item:
                    team_name = item['name']
            if 'win_probability' in data_list[1]:
                win_probability = data_list[1]['win_probability']
                points = data_list[1]['team_points']['total']
                proj_points = data_list[1]['team_projected_points']['total']
            data.update({team_name: {'win_probability': win_probability, 'points': points, 'proj_points': proj_points}})
    return data


class Fantasy:
    def __init__(self, week, reset=False, infinite=False):
        self.oauth = OAuth2(None, None, from_file='./auth/oauth2yahoo.json')
        self.base_url = 'https://fantasysports.yahooapis.com/fantasy/v2/'
        self.game_key = self.update_yahoo_game_key()
        self.league_id = '29020'
        self.week = week
        self.interval = INTERVAL
        self.inactivity_limit = TIMEOUT * (60 / self.interval)
        self.output_path = 'week_{}_scores.csv'.format(self.week)
        if reset:
            self.initialize_file()
        self.string_check = []
        self.current_string = ''
        self.infinite = infinite

    def refresh_token(self):
        if not self.oauth.token_is_valid():
            self.oauth.refresh_access_token()

    def get_json_response(self, url):
        self.refresh_token()
        response = self.oauth.session.get(url, params={'format': 'json'})
        return response.json()

    def update_yahoo_game_key(self):
        r = self.get_json_response('{}game/nfl'.format(self.base_url))
        game_key = r['fantasy_content']['game'][0]['game_key']
        return game_key

    def initialize_file(self):
        print_string = 'Timestamp,Date,Time'
        for name in FULL_TEAMS:
            for field in ['PTS', 'PROJ', 'WPCT']:
                print_string += ',{} {}'.format(name, field)
        print_string += '\n'
        with open(self.output_path, 'w') as new_file:
            new_file.write(print_string)

    def is_data_changing(self):
        if self.infinite:
            return True
        self.string_check.append(self.current_string)
        while len(self.string_check) > self.inactivity_limit:
            self.string_check.pop(0)
        if len(self.string_check) >= self.inactivity_limit and len(set(self.string_check)) == 1:
            print('Data is not changing. Stopping loop.')
            print(set(self.string_check))
            print(self.string_check)
            return False
        return True

    def collect_data(self):
        start_time = time.time()
        while self.is_data_changing():
            response = self.get_json_response('{}league/{}.l.{}/scoreboard;week={}'.format(self.base_url, self.game_key, self.league_id, self.week))
            timestamp = int(time.time())
            date_time = str(datetime.datetime.fromtimestamp(timestamp)).split()
            data = parse_data(response)
            if data:
                self.current_string = ''
                print_string = str(timestamp)
                print_string += ',' + date_time[0] + ',' + date_time[1]
                for name in TEAMS:
                    for team in data:
                        if name in team:
                            name = team
                    print_string += ',' + str(data[name]['points'])
                    print_string += ',' + str(data[name]['proj_points'])
                    print_string += ',' + str(data[name]['win_probability'])
                    self.current_string += ',' + str(data[name]['points'])
                    self.current_string += ',' + str(data[name]['proj_points'])
                    self.current_string += ',' + str(data[name]['win_probability'])
                print_string += '\n'
                with open(self.output_path, 'a') as out_file:
                    out_file.write(print_string)
            time.sleep(self.interval)
        print('Elapsed time: {}'.format(time.time() - start_time))


def main(week, reset, infinite):
    fantasy = Fantasy(week, reset, infinite)
    fantasy.collect_data()


if __name__ == '__main__':
    argv = sys.argv
    reset_flag = False
    infinite_flag = False
    if len(argv) >= 2:
        week_num = str(argv[1])
        if '-r' in argv:
            reset_flag = True
        if '-i' in argv:
            infinite_flag = True
        main(week_num, reset_flag, infinite_flag)
    else:
        print('Argument error')
