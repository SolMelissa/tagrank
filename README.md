# TagRank
is a hydrus API script that uses trueskill to figure out which tags and pictures you like best.
By making you choose which of two files you like more, over and over again, the trueskill system will figure out which tags you like most and will maintain direct MMR ratings for the pictures.
It does this by employing the same ranking algorithm that Microsoft uses for its Xbox online games.

TagRank will show you pairs of files, over and over again.
The more comparisons you make the more it learns your preferences.
You can stop at any time by pressing the `ESCape` key, your progress will be saved.
Press the `left arrow` or `A key` if you prefer the left image, the `right arrow` or `D key` for the right, and the `down arrow` or `S key` if there is no clear winner.
To go back one image pair, press `Backspace` or the `R key`. If you need to open the files externally to zoom in or pan you can press the `O key`. This will open the two files in the default program you have for that file.

TrueSkill uses these comparisons to create normal distributions for the "quality" of each tag, and a confidence score that says how sure it is of these results. The tagrankMMR Hydrus rating service stores each picture's direct comparison rating; it is not calculated by averaging tag ratings.
TagRank that uses these results to create a representation of this data.

When you are done rating games TagRank will show you the top 20 tags and their skill distributions.
The more to the right the distribution for a tag is the better, and the higher it is the more sure TagRank is of that ranking. 

## Sorting files with your ranked tags
TagRank can also create a sort order for all your images based on the tag rankings that you have created. The more images you rank the better this sort order will be. To do this, run `main.py --create_image_ranking`.

## Using the tag-ranks in your own code
If you want to do more with this data you can read it from the `data/ratings.json` file that TagRank creates.
This is a json list of `[tag_name, [mu, sigma]]` objects.
`mu` and `sigma` are the parameters for the normal distribution of that tags ranking.

TagRank stores a list of your previous comparisons in the `data/comparisons.json` file. It contains a list of lists with two file ids. First the winning id, then the losing id. It is possible that some pairs are in this list multiple times, and even in different orders. Since the list is in-order the last comparison between two file ids is the most recent. 

## Installation
- Clone the repository or download the repository in another way.
- make sure that you have python version 3.10 or higher installed.
- install the requirements in requirements.txt using pip.
- - For example, with `pip install -r requirements.txt`
- - Or with `pip install requests PySide6 matplotlib numpy scipy trueskill hydrus_api>=5.2.0`
- Now you can run main.py.


## Post-installation setup
- TagRank keeps its configuration in the `config/` directory, created automatically on first run.
- Running main.py without setting up anything else will tell you what to do next.
- - `config/KEYS`: your hydrus API access key/URL and the rating service keys TagRank writes to. This file is git-ignored since it holds secrets.
- - `config/SETTINGS`: every other tunable (search query, pool assembly, chart options), organized by section.
- - `config/TAG_FILTERS`: tag prefixes/exact tags to exclude from ranking and pool selection, one per line.
- For all of the above it holds that just running main.py and letting it figure out what it needs from you is easier than trying to do it beforehand.
