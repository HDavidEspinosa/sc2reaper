from pysc2 import run_configs
from s2clientprotocol import sc2api_pb2 as sc_pb
from pysc2.lib import point, features
from pysc2.lib.protocol import ProtocolError

from score_extraction import get_score
from action_extraction import get_actions, get_human_name
from state_extraction import get_state
from unit_extraction import get_unit_doc

from encoder import encode

import gzip
import json
from google.protobuf.json_format import MessageToDict

from pymongo import MongoClient

import multiprocessing

from absl import app, flags

FLAGS = flags.FLAGS
flags.DEFINE_string("replay", None, "Name of replay in SC2/Replays.")

STEP_MULT = 24
size = point.Point(64, 64)
interface = sc_pb.InterfaceOptions(raw=True, score=True,
                                    feature_layer=sc_pb.SpatialCameraSetup(width=24))

size.assign_to(interface.feature_layer.resolution)
size.assign_to(interface.feature_layer.minimap_resolution)

# def print_state_action_score(state, action, score):
#     # print("Summary")
#     print(f"frame: {state['frame_id']}")
#     print("Resoruces:")
#     print(state['resources'])
#     print("Supply:")
#     print(state['supply'])
#     units_string = "Units:\n"
#     for unit_type in state['units']:
#         units_string += f"{unit_type}: {len(state['units'][unit_type])}, "
#     units_string = units_string[:-2] + ""
#     print(units_string)

#     print("Units in progress:")
#     print(state['units_in_progress'])

#     print("Visible enemy units:")
#     print(state['visible_enemy_units'])

#     print("Action:")
#     print(action)

#     print("Score:")
#     print(score)

def extract_action_frames(controller, replay_data, map_data, player_id):
    '''
    This function runs through the replay once and extracts no-ops and a list
    of frames in which macro actions started being taken. This list is then 
    used in another function that runs through the replay another time and 
    considers only those positions.
    '''
    controller.start_replay(sc_pb.RequestStartReplay(
                replay_data=replay_data,
                map_data=map_data,
                options=interface,
                observed_player_id=player_id))

    abilities = controller.data_raw().abilities
    units_raw = controller.data_raw().units
    obs = controller.observe()

    controller.step(1)
    obs = controller.observe()
    # print(f"raw units: {obs.observation.raw_data.units}")

    # Extracting map information
    height_map_minimap = obs.observation.feature_layer_data.minimap_renders.height_map
    starting_location = None
    for unit in obs.observation.raw_data.units:
        unit_doc = get_unit_doc(unit)
        # print(f'unit name : {units_raw[unit_doc["unit_type"]].name}')
        if units_raw[unit_doc[e("unit_type")]].name in ["CommandCenter", "Nexus", "Hatchery"]:
            # print(f"I found a {units_raw[unit_doc['unit_type']].name}!")
            if unit.alliance == 1:
                starting_location = unit_doc[e("location")]
                break

    try:
        assert starting_location != None
    except AssertionError:
        print("Wasn't able to determine a player's starting locations, weird")
        

    map_doc_local = {
        'minimap': encode({'height_map': MessageToDict(height_map_minimap)}),
    }

    no_ops_actions = {} # a dict of action dics which is to be merged to actual macro actions.
    no_ops_states = {}
    no_ops_scores = {}
    macro_action_frames = [] # a list that will hold the frames in which macro actions START to take place. i.e. the left limit of the time interval.

    # Initialization of docs.
    initial_frame = obs.observation.game_loop

    new_actions = get_actions(obs, abilities)
    # print("I MEAN, THIS SHOULD BE PRINTED ALWAYS")
    no_ops_states[str(initial_frame)] = get_state(obs.observation, initial_frame)
    no_ops_actions[str(initial_frame)] = new_actions
    no_ops_scores[str(initial_frame)] = get_score(obs.observation)

    # running through the replay
    while True:
        try:
            controller.step(STEP_MULT)
            obs = controller.observe()
            frame_id = obs.observation.game_loop

            new_actions = get_actions(obs, abilities)
            if len(new_actions) == 0:
                # i.e. no op
                no_ops_states[str(frame_id)] = encode(get_state(obs.observation, frame_id))
                no_ops_actions[str(frame_id)] = encode(new_actions)
                no_ops_scores[str(frame_id)] = encode(get_score(obs.observation))
            if len(new_actions) > 0:
                # print("one or more macro actions was found")
                macro_action_frames.append(frame_id - STEP_MULT)
        
        except ProtocolError:
            obs = controller.observe()
            print(f"last frame recorded: {obs.observation.game_loop}")
            break

    return no_ops_states, no_ops_actions, no_ops_scores, macro_action_frames, map_doc_local, starting_location

def extract_macro_actions(controller, replay_data, map_data, player_id, macro_action_frames):
    '''
    This function takes macro_action_frames and moves through the replay only considering the places
    in which macro actions took place.
    '''
    controller.start_replay(sc_pb.RequestStartReplay(
                replay_data=replay_data,
                map_data=map_data,
                options=interface,
                observed_player_id=player_id))

    obs = controller.observe()
    abilities = controller.data_raw().abilities

    macro_actions = {} # a dict of action dics which is to be merged to the other no-ops actions.
    macro_states = {}
    macro_scores = {}
    past_frame = obs.observation.game_loop 

    for frame in macro_action_frames:
        # print(f"I'm trying to get to frame {frame}, and I'm currently in frame {past_frame}")
        if past_frame == 0:
            controller.step(frame - past_frame)
        else:
            controller.step(frame - past_frame - 1) # hot fix, I don't know why. There's just something special about 0.
        obs = controller.observe()
        # print(f"After jumping {frame-past_frame} i'm at {obs.observation.game_loop}")
        assert obs.observation.game_loop == frame
        
        for i in range(STEP_MULT): # is that +1 really necessary?
            obs = controller.observe()
            frame_id = obs.observation.game_loop

            new_actions = get_actions(obs, abilities)
            if len(new_actions) > 0:
                # i.e. if they're not no-ops:
                macro_states[str(frame_id)] = encode(get_state(obs.observation, frame_id)) # with this revamp, frame_id is unnecessary here.
                macro_actions[str(frame_id)] = encode(new_actions) # storing the whole list.
                macro_scores[str(frame_id)] = encode(get_score(obs.observation))

            # _ = input(f"Press enter to go to the next frame (current frame: {frame_id})")
            controller.step(1)

        past_frame = obs.observation.game_loop


    return macro_states, macro_actions, macro_scores


def main(unused):
    run_config = run_configs.get()

    with run_config.start() as controller:
        replay_file = FLAGS.replay
        print(f"Processing replay {replay_file}")
        replay_data = run_config.replay_data(replay_file)
        info = controller.replay_info(replay_data)

        map_data = None
        if info.local_map_path:
            map_data = run_config.map_data(info.local_map_path)
        
        # print(f"replay info: {info}")
        # Mongo experiments
        client = MongoClient('localhost', 27017)
        db = client["replays_example_4"]
        # replay_collection = db["replays"]

        # Extracting general information for the replay document

        ## Extracting the Match-up

        player_1_race = info.player_info[0].player_info.race_actual
        player_2_race = info.player_info[1].player_info.race_actual

        match_up = str(player_1_race) + "v" + str(player_2_race)
        match_up = match_up.replace("1", "T").replace("2", "Z").replace("3", "P")

        # replay_doc = {
        #     e('replay_name'): replay_file,
        #     e('match_up'): match_up,
        #     e('game_duration_loops'): info.game_duration_loops,
        #     e('game_duration_seconds'): info.game_duration_seconds,
        #     e('game_version'): info.game_version
        # }

        map_doc = {}
        map_doc["starting_location"] = {}

        for player_info in info.player_info:
            # print(player_info)
            player_id = player_info.player_info.player_id
            collection_name = replay_file.split('/')[-1]
            collection_name = collection_name.split('.')[0] + f'_{player_id}'
            player_collection = db[collection_name]
            # print(f"player info for player {player_id}: {player_info}")
            # Storing map information
            
            # Extracting info from replays
            no_ops_states, no_ops_actions, no_ops_scores, macro_action_frames, map_doc_local, starting_location = extract_action_frames(controller, replay_data, map_data, player_id)
            macro_states, macro_actions, macro_scores = extract_macro_actions(controller, replay_data, map_data, player_id, macro_action_frames)

            for key in map_doc_local:
                map_doc[key] = map_doc_local[key]

            map_doc["starting_location"][f"player_{player_id}"] = starting_location

            # Merging both
            states = {**no_ops_states, **macro_states}
            actions = {**no_ops_actions, **macro_actions}
            scores = {**no_ops_scores, **macro_scores}



            result = None
            if player_info.player_result.result == 1:
                result = 1
            elif player_info.player_result.result == 2:
                result = -1
            else:
                result = 0

            # player_doc = {
            #     e('player_id'): player_id,
            #     e('race'): str(player_info.player_info.race_actual).replace("1", "T").replace("2", "Z").replace("3", "P"),
            #     e('result'): result,
            #     e('states'): states,
            #     e('actions'): actions,
            #     e('scores'): scores
            # }

            player_info_doc = {
                'replay_name': replay_file,
                'player_id': player_id,
                'match_up': match_up,
                'game_duration_loops': info.game_duration_loops,
                'game_duration_seconds': info.game_duration_seconds,
                'game_version': info.game_version,
                'race': str(player_info.player_info.race_actual).replace("1", "T").replace("2", "Z").replace("3", "P"),
                'result': result
            }

            player_info_doc = encode(player_info_doc)

            insert = player_collection.insert_many([player_info_doc, states, actions, scores])
            print(f"Successfully filled replay collection {collection_name}")

            # Add player's doc to replay doc.
            # replay_doc[e("player_" + str(player_id))] = player_doc
        
        # result_insertion = replay_collection.insert_one(replay_doc)

        # Add map info to replay doc.
        # map_doc[e('name')] = info.map_name
        # replay_doc[e('map')] = map_doc
        # print('replay doc after encoding')
        # print(replay_doc)

        # print(f"Successfully parsed replay {replay_file}")
        # filename = replay_file.split('/')[-1]
        # filename = filename.split('.')[0]

        # with gzip.GzipFile(f'{filename}gziped.json', 'w') as fout:
        #     fout.write(json.dumps(replay_doc).encode('utf-8'))
        # print(f"Successfully wrote {filename}gziped.json file.")

        # with open(f'{filename}encoded.json', 'w') as output:
        #     json.dump(replay_doc, output)
        # print(f"Successfully wrote {filename}encoded.json file.")

        # print(f"replay doc: {replay_doc}")

        

if __name__ == '__main__':
    app.run(main)
